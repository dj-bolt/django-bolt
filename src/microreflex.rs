//! Rust state synchronization for micro-reflex.
//!
//! The user's event handlers and state objects stay in Python (that is the
//! Reflex developer experience). What lives here is the per-session
//! bookkeeping around them — the LiveView-style split applied inside the
//! server:
//!
//! - `RxPage`: the page's slot table, registered ONCE at `add_page()` with
//!   the JSON patch prefix for every slot precomputed ("do it once at
//!   registration, reuse forever at runtime").
//! - `RxSession`: one per WebSocket connection — holds the render cache
//!   (last value sent to that client per slot), diffs freshly rendered
//!   values against it, and emits the complete `{"t":"p","p":[...]}` wire
//!   frame with serde_json escaping. No Python dicts, no Python string
//!   compares, no Python JSON encoding on the per-event path.
//!
//! Python's per-event work shrinks to evaluating slot values (which calls
//! user code) and handing the list to `RxSession::diff`.

use std::sync::Arc;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyFloat, PyInt, PyList, PyString};

const FRAME_HEADER: &str = "{\"t\":\"p\",\"p\":[";

/// Last-sent value for one slot.
#[derive(Clone, PartialEq)]
enum CachedValue {
    Str(String),
    Bool(bool),
    Int(i64),
    Float(f64),
    None,
}

impl CachedValue {
    fn write_json(&self, out: &mut Vec<u8>) {
        match self {
            CachedValue::Str(s) => {
                // serde_json escapes directly into the frame buffer (its
                // table-driven escaper, no intermediate String).
                serde_json::to_writer(&mut *out, s).expect("writing to Vec is infallible");
            }
            CachedValue::Bool(b) => out.extend_from_slice(if *b { b"true" } else { b"false" }),
            CachedValue::Int(i) => out.extend_from_slice(i.to_string().as_bytes()),
            CachedValue::Float(f) => {
                serde_json::to_writer(&mut *out, f).expect("writing to Vec is infallible");
            }
            CachedValue::None => out.extend_from_slice(b"null"),
        }
    }
}

/// A page's compiled slot table: the constant JSON prefix for each slot's
/// patch object, in slot order (e.g. `{"k":"t","i":3,"v":`).
struct SlotTable {
    prefixes: Vec<String>,
}

/// Compiled per-page slot table. Frozen: built once at registration.
#[pyclass(frozen, module = "django_bolt._core")]
pub struct RxPage {
    table: Arc<SlotTable>,
}

#[pymethods]
impl RxPage {
    /// spec: one `(kind, slot_id, attr_name, elem_id)` tuple per slot, in
    /// slot order. kind: 0=text, 1=html, 2=attr. attr_name/elem_id are only
    /// meaningful for kind=2.
    #[new]
    fn new(spec: Vec<(u8, u32, Option<String>, u32)>) -> PyResult<Self> {
        let prefixes = spec
            .into_iter()
            .map(|(kind, slot_id, attr_name, elem_id)| match kind {
                0 => Ok(format!("{{\"k\":\"t\",\"i\":{},\"v\":", slot_id)),
                1 => Ok(format!("{{\"k\":\"h\",\"i\":{},\"v\":", slot_id)),
                2 => {
                    let name = attr_name.ok_or_else(|| {
                        PyValueError::new_err("attr slot requires an attribute name")
                    })?;
                    let encoded = serde_json::to_string(&name)
                        .map_err(|e| PyValueError::new_err(e.to_string()))?;
                    Ok(format!(
                        "{{\"k\":\"a\",\"e\":{},\"a\":{},\"v\":",
                        elem_id, encoded
                    ))
                }
                other => Err(PyValueError::new_err(format!(
                    "unknown slot kind: {other} (expected 0=text, 1=html, 2=attr)"
                ))),
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok(RxPage {
            table: Arc::new(SlotTable { prefixes }),
        })
    }

    /// Number of slots in the page.
    fn __len__(&self) -> usize {
        self.table.prefixes.len()
    }

    /// Create a fresh per-connection render cache for this page.
    fn new_session(&self) -> RxSession {
        RxSession {
            table: Arc::clone(&self.table),
            cache: vec![None; self.table.prefixes.len()],
        }
    }
}

/// Per-connection render cache + differ.
#[pyclass(module = "django_bolt._core")]
pub struct RxSession {
    table: Arc<SlotTable>,
    cache: Vec<Option<CachedValue>>,
}

impl RxSession {
    fn check_len(&self, values: &Bound<'_, PyList>) -> PyResult<()> {
        if values.len() != self.table.prefixes.len() {
            return Err(PyValueError::new_err(format!(
                "expected {} slot values, got {}",
                self.table.prefixes.len(),
                values.len()
            )));
        }
        Ok(())
    }
}

fn extract_value(value: &Bound<'_, PyAny>) -> PyResult<CachedValue> {
    // bool before int: Python bool is an int subtype.
    if let Ok(b) = value.cast::<PyBool>() {
        return Ok(CachedValue::Bool(b.is_true()));
    }
    if value.is_none() {
        return Ok(CachedValue::None);
    }
    if let Ok(s) = value.cast::<PyString>() {
        return Ok(CachedValue::Str(s.to_str()?.to_owned()));
    }
    if let Ok(i) = value.cast::<PyInt>() {
        return Ok(CachedValue::Int(i.extract::<i64>()?));
    }
    if let Ok(f) = value.cast::<PyFloat>() {
        return Ok(CachedValue::Float(f.extract::<f64>()?));
    }
    Err(PyTypeError::new_err(format!(
        "unsupported slot value type: {} (expected str, bool, int, float, or None)",
        value.get_type().name()?
    )))
}

/// True when the freshly rendered Python value equals the cached one,
/// WITHOUT copying strings (the common no-change case allocates nothing).
fn value_equals_cached(value: &Bound<'_, PyAny>, cached: &CachedValue) -> PyResult<bool> {
    Ok(match cached {
        CachedValue::Str(old) => match value.cast::<PyString>() {
            Ok(s) => s.to_str()? == old.as_str(),
            Err(_) => false,
        },
        CachedValue::Bool(old) => match value.cast::<PyBool>() {
            Ok(b) => b.is_true() == *old,
            Err(_) => false,
        },
        CachedValue::Int(old) => {
            !value.cast::<PyBool>().is_ok()
                && matches!(value.cast::<PyInt>(), Ok(i) if i.extract::<i64>()? == *old)
        }
        CachedValue::Float(old) => {
            matches!(value.cast::<PyFloat>(), Ok(f) if f.extract::<f64>()? == *old)
        }
        CachedValue::None => value.is_none(),
    })
}

#[pymethods]
impl RxSession {
    /// Fill the cache to match the pre-rendered initial document, sending
    /// nothing. Called once at connection setup.
    fn prime(&mut self, values: &Bound<'_, PyList>) -> PyResult<()> {
        self.check_len(values)?;
        for (index, value) in values.iter().enumerate() {
            self.cache[index] = Some(extract_value(&value)?);
        }
        Ok(())
    }

    /// Diff freshly rendered slot values against what this client last saw.
    /// Returns the complete `{"t":"p","p":[...]}` wire frame, or None when
    /// nothing changed. With force=true every slot is emitted (client resync
    /// after reconnect).
    #[pyo3(signature = (values, force = false))]
    fn diff(&mut self, values: &Bound<'_, PyList>, force: bool) -> PyResult<Option<String>> {
        self.check_len(values)?;
        let mut frame: Option<Vec<u8>> = None;
        for (index, value) in values.iter().enumerate() {
            if !force {
                if let Some(cached) = &self.cache[index] {
                    if value_equals_cached(&value, cached)? {
                        continue;
                    }
                }
            }
            let extracted = extract_value(&value)?;
            let buf = match frame.as_mut() {
                Some(buf) => {
                    buf.push(b',');
                    buf
                }
                None => {
                    let buf = frame.insert(Vec::with_capacity(128));
                    buf.extend_from_slice(FRAME_HEADER.as_bytes());
                    buf
                }
            };
            buf.extend_from_slice(self.table.prefixes[index].as_bytes());
            extracted.write_json(buf);
            buf.push(b'}');
            self.cache[index] = Some(extracted);
        }
        match frame {
            Some(mut buf) => {
                buf.extend_from_slice(b"]}");
                let text = String::from_utf8(buf)
                    .map_err(|e| PyValueError::new_err(format!("frame is not UTF-8: {e}")))?;
                Ok(Some(text))
            }
            None => Ok(None),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table() -> Arc<SlotTable> {
        Arc::new(SlotTable {
            prefixes: vec![
                "{\"k\":\"t\",\"i\":0,\"v\":".to_string(),
                "{\"k\":\"a\",\"e\":1,\"a\":\"disabled\",\"v\":".to_string(),
            ],
        })
    }

    #[test]
    fn frame_building_and_escaping() {
        let mut out = FRAME_HEADER.as_bytes().to_vec();
        out.extend_from_slice(table().prefixes[0].as_bytes());
        CachedValue::Str("a\"<b>\n".to_string()).write_json(&mut out);
        out.push(b'}');
        out.extend_from_slice(b"]}");
        let parsed: serde_json::Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(parsed["p"][0]["v"], "a\"<b>\n");
        assert_eq!(parsed["t"], "p");
    }

    #[test]
    fn scalar_values_serialize_as_json_types() {
        for (value, expected) in [
            (CachedValue::Bool(true), "true"),
            (CachedValue::Int(-42), "-42"),
            (CachedValue::None, "null"),
        ] {
            let mut out = Vec::new();
            value.write_json(&mut out);
            assert_eq!(out, expected.as_bytes());
        }

        // string escaping is delegated to serde_json and must round-trip
        for input in ["plain", "q\" b\\ n\n", "unicode: héllo 世界 🚀", ""] {
            let mut out = Vec::new();
            CachedValue::Str(input.to_string()).write_json(&mut out);
            assert_eq!(out, serde_json::to_string(input).unwrap().as_bytes());
        }
    }
}
