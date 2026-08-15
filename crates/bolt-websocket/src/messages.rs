//! WebSocket message types for communication between Actix actor and Python handler

use actix::Message;

/// Message types for communication between Actix actor and Python handler
#[derive(Debug)]
pub enum WsMessage {
    /// Text message from client
    Text(String),
    /// Binary message from client
    Binary(Vec<u8>),
    /// Client disconnected
    Disconnect { code: u16 },
    /// Connection accepted by Python handler
    Accept {
        #[allow(dead_code)] // Reserved for subprotocol negotiation
        subprotocol: Option<String>,
    },
    /// Send text to client (from Python)
    SendText(String),
    /// Send binary to client (from Python)
    SendBinary(Vec<u8>),
    /// Close connection (from Python)
    Close { code: u16, reason: String },
}

/// Actix message for sending data to client
#[derive(Message)]
#[rtype(result = "()")]
pub struct SendToClient(pub WsMessage);
