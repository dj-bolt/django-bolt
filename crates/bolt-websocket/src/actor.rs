//! WebSocket actor that bridges Actix and Python
//!
//! Handles the WebSocket connection lifecycle, heartbeat, and message forwarding.

use actix::{Actor, ActorContext, AsyncContext, Handler, StreamHandler};
use actix_web_actors::ws;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

use super::config::WS_CONFIG;
use super::messages::{SendToClient, WsMessage};
use super::ACTIVE_WS_CONNECTIONS;

/// How often each connection polls the process-wide drain flag.
const DRAIN_POLL_INTERVAL: Duration = Duration::from_millis(250);

/// WebSocket actor that bridges Actix and Python
pub struct WebSocketActor {
    /// Client heartbeat tracking
    hb: Instant,
    /// Channel to send received messages to Python handler
    to_python_tx: mpsc::Sender<WsMessage>,
    /// Whether connection has been accepted
    accepted: bool,
    /// Close code if connection was closed
    close_code: Option<u16>,
    /// Heartbeat interval (from cached config)
    heartbeat_interval: Duration,
    /// Client timeout (from cached config)
    client_timeout: Duration,
    /// Max message size (from cached config)
    max_message_size: usize,
}

impl WebSocketActor {
    pub fn new(to_python_tx: mpsc::Sender<WsMessage>) -> Self {
        // Use cached config - no Python/GIL access here
        let config = &*WS_CONFIG;
        WebSocketActor {
            hb: Instant::now(),
            to_python_tx,
            accepted: false,
            close_code: None,
            heartbeat_interval: config.heartbeat_interval,
            client_timeout: config.client_timeout,
            max_message_size: config.max_message_size,
        }
    }

    /// Start heartbeat process
    fn start_heartbeat(&self, ctx: &mut ws::WebsocketContext<Self>) {
        let timeout = self.client_timeout;
        ctx.run_interval(self.heartbeat_interval, move |act, ctx| {
            if Instant::now().duration_since(act.hb) > timeout {
                // Send disconnect to Python
                let _ = act
                    .to_python_tx
                    .try_send(WsMessage::Disconnect { code: 1001 });
                ctx.stop();
                return;
            }
            ctx.ping(b"");
        });
    }

    /// Poll the process-wide drain flag; when the server begins shutting
    /// down (stop or recycle), close the connection with 1012 (Service
    /// Restart) so clients know to reconnect — they'll land on a healthy
    /// worker via SO_REUSEPORT.
    fn start_drain_watch(&self, ctx: &mut ws::WebsocketContext<Self>) {
        ctx.run_interval(DRAIN_POLL_INTERVAL, |act, ctx| {
            if super::is_draining() && act.close_code.is_none() {
                act.close_code = Some(1012);
                ctx.close(Some(ws::CloseReason {
                    code: ws::CloseCode::Restart,
                    description: Some("server restarting".to_string()),
                }));
                ctx.stop();
            }
        });
    }

    /// Close connection with error code when Python channel fails
    fn close_on_channel_error(&mut self, ctx: &mut ws::WebsocketContext<Self>, error: &str) {
        eprintln!("[django-bolt] {}", error);
        self.close_code = Some(1011); // Internal error
        ctx.close(Some(ws::CloseReason {
            code: ws::CloseCode::Error,
            description: Some("Internal server error".to_string()),
        }));
        ctx.stop();
    }
}

impl Actor for WebSocketActor {
    type Context = ws::WebsocketContext<Self>;

    fn started(&mut self, ctx: &mut Self::Context) {
        self.start_heartbeat(ctx);
        self.start_drain_watch(ctx);
    }

    fn stopped(&mut self, _ctx: &mut Self::Context) {
        // Notify Python handler that connection is closed
        let code = self.close_code.unwrap_or(1000);
        let _ = self.to_python_tx.try_send(WsMessage::Disconnect { code });

        // Decrement active connection counter
        ACTIVE_WS_CONNECTIONS.fetch_sub(1, Ordering::Relaxed);
    }
}

/// Handle messages from Python to send to client
impl Handler<SendToClient> for WebSocketActor {
    type Result = ();

    fn handle(&mut self, msg: SendToClient, ctx: &mut Self::Context) {
        match msg.0 {
            WsMessage::Accept { subprotocol: _ } => {
                self.accepted = true;
                // Accept is implicit in Actix - connection is already open
            }
            WsMessage::SendText(text) => {
                if self.accepted {
                    ctx.text(text);
                }
            }
            WsMessage::SendBinary(data) => {
                if self.accepted {
                    ctx.binary(data);
                }
            }
            WsMessage::Close { code, reason } => {
                self.close_code = Some(code);
                ctx.close(Some(ws::CloseReason {
                    code: ws::CloseCode::Other(code),
                    description: if reason.is_empty() {
                        None
                    } else {
                        Some(reason)
                    },
                }));
                ctx.stop();
            }
            _ => {}
        }
    }
}

/// Handle incoming WebSocket messages from client
impl StreamHandler<Result<ws::Message, ws::ProtocolError>> for WebSocketActor {
    fn handle(&mut self, msg: Result<ws::Message, ws::ProtocolError>, ctx: &mut Self::Context) {
        match msg {
            Ok(ws::Message::Ping(msg)) => {
                self.hb = Instant::now();
                ctx.pong(&msg);
            }
            Ok(ws::Message::Pong(_)) => {
                self.hb = Instant::now();
            }
            Ok(ws::Message::Text(text)) => {
                self.hb = Instant::now();
                // Validate message size
                if text.len() > self.max_message_size {
                    eprintln!(
                        "[django-bolt] WebSocket message too large: {} bytes (max {})",
                        text.len(),
                        self.max_message_size
                    );
                    self.close_code = Some(1009); // Message too big
                    ctx.close(Some(ws::CloseReason {
                        code: ws::CloseCode::Size,
                        description: Some("Message too large".to_string()),
                    }));
                    ctx.stop();
                    return;
                }
                // Forward to Python handler - close connection if channel fails
                if let Err(e) = self
                    .to_python_tx
                    .try_send(WsMessage::Text(text.to_string()))
                {
                    self.close_on_channel_error(
                        ctx,
                        &format!("Failed to forward text message to Python: {}", e),
                    );
                }
            }
            Ok(ws::Message::Binary(bin)) => {
                self.hb = Instant::now();
                // Validate message size
                if bin.len() > self.max_message_size {
                    eprintln!(
                        "[django-bolt] WebSocket binary message too large: {} bytes (max {})",
                        bin.len(),
                        self.max_message_size
                    );
                    self.close_code = Some(1009);
                    ctx.close(Some(ws::CloseReason {
                        code: ws::CloseCode::Size,
                        description: Some("Message too large".to_string()),
                    }));
                    ctx.stop();
                    return;
                }
                // Forward to Python handler - close connection if channel fails
                if let Err(e) = self.to_python_tx.try_send(WsMessage::Binary(bin.to_vec())) {
                    self.close_on_channel_error(
                        ctx,
                        &format!("Failed to forward binary message to Python: {}", e),
                    );
                }
            }
            Ok(ws::Message::Close(reason)) => {
                let code = reason
                    .as_ref()
                    .map(|r| match r.code {
                        ws::CloseCode::Normal => 1000,
                        ws::CloseCode::Away => 1001,
                        ws::CloseCode::Protocol => 1002,
                        ws::CloseCode::Unsupported => 1003,
                        ws::CloseCode::Abnormal => 1006,
                        ws::CloseCode::Invalid => 1007,
                        ws::CloseCode::Policy => 1008,
                        ws::CloseCode::Size => 1009,
                        ws::CloseCode::Extension => 1010,
                        ws::CloseCode::Error => 1011,
                        ws::CloseCode::Restart => 1012,
                        ws::CloseCode::Again => 1013,
                        ws::CloseCode::Other(c) => c,
                        _ => 1000, // Unknown close codes default to normal
                    })
                    .unwrap_or(1000);
                self.close_code = Some(code);
                let _ = self.to_python_tx.try_send(WsMessage::Disconnect { code });
                ctx.close(reason);
                ctx.stop();
            }
            Err(e) => {
                eprintln!("[django-bolt] WebSocket protocol error: {}", e);
                self.close_code = Some(1002); // Protocol error
                let _ = self
                    .to_python_tx
                    .try_send(WsMessage::Disconnect { code: 1002 });
                ctx.stop();
            }
            _ => {}
        }
    }
}
