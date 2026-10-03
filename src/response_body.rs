use pyo3::pybacked::PyBackedBytes;

#[cfg(not(Py_GIL_DISABLED))]
pub(crate) type AttachedBytes = PyBackedBytes;

/// Keep Python storage alive until the last response reference is released.
#[cfg(Py_GIL_DISABLED)]
pub(crate) struct AttachedBytes(Option<PyBackedBytes>);

#[cfg(Py_GIL_DISABLED)]
impl From<PyBackedBytes> for AttachedBytes {
    fn from(bytes: PyBackedBytes) -> Self {
        Self(Some(bytes))
    }
}

#[cfg(Py_GIL_DISABLED)]
impl AsRef<[u8]> for AttachedBytes {
    fn as_ref(&self) -> &[u8] {
        self.0.as_ref().expect("response bytes are alive").as_ref()
    }
}

#[cfg(Py_GIL_DISABLED)]
impl Drop for AttachedBytes {
    fn drop(&mut self) {
        // Release the reference here instead of adding it to PyO3's global queue.
        // If Python cannot attach during shutdown, retain PyO3's normal cleanup behavior.
        pyo3::Python::try_attach(|_| drop(self.0.take()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::Bytes;
    use pyo3::prelude::*;
    use pyo3::types::PyBytes;
    #[cfg(Py_GIL_DISABLED)]
    use pyo3::types::PyModule;
    #[cfg(Py_GIL_DISABLED)]
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };

    #[cfg(Py_GIL_DISABLED)]
    #[pyclass]
    struct Released(Arc<AtomicBool>);

    #[cfg(Py_GIL_DISABLED)]
    impl Drop for Released {
        fn drop(&mut self) {
            self.0.store(true, Ordering::SeqCst);
        }
    }

    #[test]
    fn keeps_python_storage_for_clones_and_slices() {
        Python::initialize();
        let body = Python::attach(|py| {
            let original = PyBytes::new(py, b"response body");
            let ptr = original.as_bytes().as_ptr();
            let backed = original.extract::<PyBackedBytes>().unwrap();
            let body = Bytes::from_owner(AttachedBytes::from(backed));
            assert_eq!(body.as_ptr(), ptr);
            body
        });
        let clone = body.clone();
        let slice = body.slice(9..);
        assert_eq!(slice.as_ptr(), body.as_ptr().wrapping_add(9));
        drop(body);
        std::thread::spawn(move || {
            assert_eq!(&clone[..], b"response body");
            drop(clone);
            assert_eq!(&slice[..], b"body");
        })
        .join()
        .unwrap();
    }

    #[cfg(Py_GIL_DISABLED)]
    #[test]
    fn releases_storage_before_another_python_attachment() {
        Python::initialize();
        let released = std::thread::spawn(|| {
            let flag = Arc::new(AtomicBool::new(false));
            let body = Python::attach(|py| {
                let module = PyModule::from_code(
                    py,
                    c"class Body(bytes):\n    pass\n",
                    c"body_lifetime.py",
                    c"body_lifetime",
                )
                .unwrap();
                let original = module
                    .getattr("Body")
                    .unwrap()
                    .call1((b"response body".as_slice(),))
                    .unwrap();
                original
                    .setattr("released", Py::new(py, Released(flag.clone())).unwrap())
                    .unwrap();
                let backed = original.extract::<PyBackedBytes>().unwrap();
                Bytes::from_owner(AttachedBytes::from(backed))
            });
            let clone = body.clone();
            drop(body);
            assert!(!flag.load(Ordering::SeqCst));
            drop(clone);
            flag.load(Ordering::SeqCst)
        })
        .join()
        .unwrap();
        assert!(
            released,
            "body cleanup waited for a later Python attachment"
        );
    }
}
