//! Find a Bolt Serializer in a response value before msgspec encodes it.
//!
//! msgspec encodes each Struct natively, so a Serializer that a container
//! holds would send its write-only fields. `SerializerScanEncoder.encode`
//! scans the value first. It calls the Python dump only when the scan finds a
//! Serializer, so a value with no Serializer costs one native pass.

use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
#[cfg(Py_GIL_DISABLED)]
use pyo3::types::PyList;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PySet, PyString, PyTuple,
};

/// The deepest level that the scan visits. At this level the scan answers
/// true, and the Python walk then meets the recursion limit, as msgspec does.
/// The scan keeps its own stack, so a deep or cyclic value cannot overflow
/// the thread stack.
const MAX_DEPTH: usize = 10_000;

/// A value of an exact builtin type that cannot hold a Serializer.
#[inline]
fn is_exact_leaf(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyFloat>()
}

/// `is_exact_leaf` for a borrowed pointer.
///
/// # Safety
///
/// `value` must point to a live Python object.
#[cfg(not(Py_GIL_DISABLED))]
#[inline]
unsafe fn is_exact_leaf_ptr(value: *mut pyo3::ffi::PyObject) -> bool {
    use pyo3::ffi;
    unsafe {
        value == ffi::Py_None()
            || ffi::PyUnicode_CheckExact(value) != 0
            || ffi::PyLong_CheckExact(value) != 0
            || ffi::PyBool_Check(value) != 0
            || ffi::PyFloat_CheckExact(value) != 0
    }
}

/// A value of a subclass of a scalar type, for example a str Enum.
#[inline]
fn is_scalar_subclass(value: &Bound<'_, PyAny>) -> bool {
    value.is_instance_of::<PyString>()
        || value.is_instance_of::<PyInt>()
        || value.is_instance_of::<PyFloat>()
        || value.is_instance_of::<PyBytes>()
        || value.is_instance_of::<PyByteArray>()
}

/// The state of one scan: the objects to visit, and the last type that
/// holds no Serializer.
struct Scan<'a, 'py> {
    stack: Vec<(Bound<'py, PyAny>, usize)>,
    kinds: &'a Bound<'py, PyDict>,
    classify: &'a Bound<'py, PyAny>,
    /// The last type that `kinds` marks as a leaf. A child of this type is
    /// skipped with one pointer compare, so a list of one Struct type reads
    /// its kind once. The value holds an instance of the type during the
    /// scan, so the type stays alive.
    leaf_type: *mut pyo3::ffi::PyTypeObject,
}

impl<'a, 'py> Scan<'a, 'py> {
    /// Return whether the scan must visit an object of type `item_type`
    /// that is not an exact leaf. A type that `kinds` marks as a leaf
    /// becomes `leaf_type`.
    #[inline]
    fn wants_type(&mut self, item_type: *mut pyo3::ffi::PyTypeObject) -> bool {
        use pyo3::ffi;
        if item_type == self.leaf_type {
            return false;
        }
        let is_builtin_container = {
            item_type == &raw mut ffi::PyDict_Type
                || item_type == &raw mut ffi::PyList_Type
                || item_type == &raw mut ffi::PyTuple_Type
        };
        if is_builtin_container {
            return true;
        }
        // SAFETY: `item_type` is the type of a live object, so it is alive.
        let item_type_object =
            unsafe { Bound::from_borrowed_ptr(self.kinds.py(), item_type.cast()) };
        match self.kinds.get_item(&item_type_object) {
            Ok(Some(kind)) if kind.is_none() => {
                self.leaf_type = item_type;
                false
            }
            // An unknown type, a Serializer or a type with fields: `visit`
            // reads the kind again and raises an error of the lookup.
            _ => true,
        }
    }

    #[inline]
    fn push(&mut self, child: Bound<'py, PyAny>, depth: usize) {
        if !is_exact_leaf(&child) && self.wants_type(child.get_type().as_type_ptr()) {
            self.stack.push((child, depth));
        }
    }

    /// Put the items of a dict, list or tuple on the stack. Return false for
    /// any other value.
    ///
    /// With the GIL, the loops read borrowed pointers, so a leaf item costs no
    /// reference count change.
    #[cfg(not(Py_GIL_DISABLED))]
    fn push_items(&mut self, item: &Bound<'py, PyAny>, depth: usize) -> bool {
        use pyo3::ffi;
        let py = item.py();
        let container = item.as_ptr();
        // SAFETY: the thread holds the GIL. The loops run no Python code: a
        // kind lookup hashes and compares type objects, which is native.
        // Thus no thread can change the container while a loop reads it, and
        // each borrowed item stays alive. An item goes on the stack as a new
        // reference.
        unsafe {
            if ffi::PyDict_Check(container) != 0 {
                let mut position: ffi::Py_ssize_t = 0;
                let mut key = std::ptr::null_mut();
                let mut value = std::ptr::null_mut();
                while ffi::PyDict_Next(container, &mut position, &mut key, &mut value) != 0 {
                    if !is_exact_leaf_ptr(value) && self.wants_type(ffi::Py_TYPE(value)) {
                        self.stack
                            .push((Bound::from_borrowed_ptr(py, value), depth));
                    }
                }
                return true;
            }
            let (size, get_item): (
                unsafe extern "C" fn(*mut ffi::PyObject) -> ffi::Py_ssize_t,
                unsafe extern "C" fn(*mut ffi::PyObject, ffi::Py_ssize_t) -> *mut ffi::PyObject,
            ) = if ffi::PyList_Check(container) != 0 {
                (ffi::PyList_Size, ffi::PyList_GetItem)
            } else if ffi::PyTuple_Check(container) != 0 {
                (ffi::PyTuple_Size, ffi::PyTuple_GetItem)
            } else {
                return false;
            };
            for index in 0..size(container) {
                let child = get_item(container, index);
                if !is_exact_leaf_ptr(child) && self.wants_type(ffi::Py_TYPE(child)) {
                    self.stack
                        .push((Bound::from_borrowed_ptr(py, child), depth));
                }
            }
        }
        true
    }

    /// Put the items of a dict, list or tuple on the stack. Return false for
    /// any other value.
    ///
    /// Without the GIL, another thread can change a container. The PyO3
    /// iterators hold a strong reference to each item.
    #[cfg(Py_GIL_DISABLED)]
    fn push_items(&mut self, item: &Bound<'py, PyAny>, depth: usize) -> bool {
        if let Ok(dict) = item.cast::<PyDict>() {
            dict.iter().for_each(|(_, child)| self.push(child, depth));
        } else if let Ok(list) = item.cast::<PyList>() {
            list.iter().for_each(|child| self.push(child, depth));
        } else if let Ok(tuple) = item.cast::<PyTuple>() {
            tuple.iter().for_each(|child| self.push(child, depth));
        } else {
            return false;
        }
        true
    }

    /// Put the children of `item` on the stack at `depth`. Return true if
    /// `item` is a Serializer.
    fn visit(&mut self, item: &Bound<'py, PyAny>, depth: usize) -> PyResult<bool> {
        if self.push_items(item, depth) {
            return Ok(false);
        }
        if let Ok(set) = item.cast::<PySet>() {
            set.iter().for_each(|child| self.push(child, depth));
            return Ok(false);
        }
        if let Ok(set) = item.cast::<PyFrozenSet>() {
            set.iter().for_each(|child| self.push(child, depth));
            return Ok(false);
        }
        let item_type = item.get_type();
        if is_scalar_subclass(item) {
            self.leaf_type = item_type.as_type_ptr();
            return Ok(false);
        }
        let kind = match self.kinds.get_item(&item_type)? {
            Some(kind) => kind,
            None => self.classify.call1((&item_type,))?,
        };
        if kind.is_none() {
            self.leaf_type = item_type.as_type_ptr();
            return Ok(false);
        }
        let Ok(names) = kind.cast::<PyTuple>() else {
            return Ok(true);
        };
        for name in names.iter() {
            match item.getattr(name.cast::<PyString>()?) {
                Ok(child) => self.push(child, depth),
                // msgspec skips an attribute that the object does not have.
                Err(err) if err.is_instance_of::<PyAttributeError>(item.py()) => {}
                Err(err) => return Err(err),
            }
        }
        Ok(false)
    }
}

/// Return whether `value` holds an object whose kind marks a Serializer.
///
/// The scan visits the items of dicts, lists, tuples, sets and frozensets.
/// For each other object, `kinds` maps its type to a kind:
///
/// - `None`: the scan does not visit the object.
/// - a tuple of attribute names: the scan visits these attributes.
/// - any other value (Python gives `True`): the object is a Serializer.
///
/// `classify(type)` gives the kind of a type that `kinds` does not hold.
#[pyfunction]
pub fn find_serializer<'py>(
    value: &Bound<'py, PyAny>,
    kinds: &Bound<'py, PyDict>,
    classify: &Bound<'py, PyAny>,
) -> PyResult<bool> {
    if is_exact_leaf(value) {
        return Ok(false);
    }
    // The stack allocates only when the value has a child to visit.
    let mut scan = Scan {
        stack: Vec::new(),
        kinds,
        classify,
        leaf_type: std::ptr::null_mut(),
    };
    if scan.visit(value, 1)? {
        return Ok(true);
    }
    while let Some((item, depth)) = scan.stack.pop() {
        if depth >= MAX_DEPTH || scan.visit(&item, depth + 1)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// A JSON encoder that dumps each Serializer in the value first.
///
/// `encode(value)` scans the value. If the value holds a Serializer, it
/// encodes `dump(value)`. Otherwise it encodes the value as it is.
#[pyclass(frozen)]
pub struct SerializerScanEncoder {
    encode: Py<PyAny>,
    kinds: Py<PyDict>,
    classify: Py<PyAny>,
    dump: Py<PyAny>,
}

#[pymethods]
impl SerializerScanEncoder {
    #[new]
    fn new(encode: Py<PyAny>, kinds: Py<PyDict>, classify: Py<PyAny>, dump: Py<PyAny>) -> Self {
        Self {
            encode,
            kinds,
            classify,
            dump,
        }
    }

    fn encode<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let encode = self.encode.bind(py);
        if find_serializer(value, self.kinds.bind(py), self.classify.bind(py))? {
            return encode.call1((self.dump.bind(py).call1((value,))?,));
        }
        encode.call1((value,))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use pyo3::types::{PyList, PyModule};

    const FIXTURES: &std::ffi::CStr = c"
class Secret:
    pass

class Holder:
    def __init__(self, inner):
        self.inner = inner

class Empty:
    pass

class Text(str):
    pass

calls = []
KINDS = {Holder: ('inner', 'missing')}

def classify(tp):
    calls.append(tp)
    if tp is Secret:
        kind = True
    elif tp is Holder:
        kind = ('inner', 'missing')
    else:
        kind = None
    KINDS[tp] = kind
    return kind

def kinds():
    return KINDS
";

    /// Run the fixtures in a new module for each call.
    ///
    /// `PyModule::from_code` runs the code in the module of `sys.modules`
    /// with the same name. Tests run in parallel threads, so a test could
    /// then replace the classes of another test while it runs.
    fn fixtures(py: Python<'_>) -> Bound<'_, PyModule> {
        let module = PyModule::new(py, "scan_fixtures").unwrap();
        py.run(FIXTURES, Some(&module.dict()), None).unwrap();
        module
    }

    fn scan(module: &Bound<'_, PyModule>, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        let kinds = module.getattr("kinds").unwrap().call0().unwrap();
        let classify = module.getattr("classify").unwrap();
        find_serializer(value, kinds.cast::<PyDict>().unwrap(), &classify)
    }

    fn eval<'py>(module: &Bound<'py, PyModule>, expr: &str) -> Bound<'py, PyAny> {
        let code = std::ffi::CString::new(expr).unwrap();
        module.py().eval(&code, Some(&module.dict()), None).unwrap()
    }

    #[test]
    fn finds_a_serializer_in_each_container() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            for expr in [
                "Secret()",
                "{'a': Secret()}",
                "[1, 'x', {'b': [Secret()]}]",
                "(None, Secret())",
                "{'s': frozenset([Secret()])}",
                "[{Secret()}]",
                "Holder(Secret())",
                "[Holder(Holder(Secret()))]",
                "{'k': Holder({'deep': (Secret(),)})}",
            ] {
                assert!(scan(&module, &eval(&module, expr)).unwrap(), "{expr}");
            }
        });
    }

    #[test]
    fn finds_no_serializer_in_plain_values() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            for expr in [
                "None",
                "1",
                "'x'",
                "Text('x')",
                "b'raw'",
                "[1, 2.5, True, None, 'x', Text('y')]",
                "{'a': {'b': [(), set(), frozenset()]}}",
                "Empty()",
                "[Empty(), {'e': Empty()}]",
                "Holder(1)",
                "Holder(Empty())",
            ] {
                assert!(!scan(&module, &eval(&module, expr)).unwrap(), "{expr}");
            }
        });
    }

    #[test]
    fn asks_classify_only_for_a_type_that_kinds_does_not_hold() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let value = eval(&module, "[Holder(Empty()), Text('x'), 1, {'a': None}]");
            assert!(!scan(&module, &value).unwrap());
            let calls = eval(&module, "[tp.__name__ for tp in calls]");
            assert_eq!(calls.extract::<Vec<String>>().unwrap(), vec!["Empty"]);
        });
    }

    #[test]
    fn a_leaf_type_hides_no_other_type() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let value = eval(&module, "[Empty(), Empty(), {'e': Empty()}, (Empty(),)]");
            assert!(!scan(&module, &value).unwrap());
            let calls = eval(&module, "[tp.__name__ for tp in calls]");
            assert_eq!(calls.extract::<Vec<String>>().unwrap(), vec!["Empty"]);
            for expr in [
                "[Empty(), Empty(), Secret()]",
                "[Empty(), Holder(Secret())]",
            ] {
                assert!(scan(&module, &eval(&module, expr)).unwrap(), "{expr}");
            }
        });
    }

    #[test]
    fn a_classify_error_reaches_the_caller() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let value = eval(&module, "[Empty()]");
            let kinds = PyDict::new(py);
            let classify = eval(&module, "lambda tp: 1 / 0");
            let err = find_serializer(&value, &kinds, &classify).unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyZeroDivisionError>(py));
        });
    }

    #[test]
    fn a_value_deeper_than_the_limit_counts_as_found() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let mut deep = PyList::empty(py).into_any();
            for _ in 0..MAX_DEPTH {
                deep = PyList::new(py, [deep]).unwrap().into_any();
            }
            assert!(scan(&module, &deep).unwrap());
            let cyclic = eval(&module, "(lambda a: (a.append(a), a)[1])([])");
            assert!(scan(&module, &cyclic).unwrap());

            let mut shallow = PyList::empty(py).into_any();
            for _ in 0..MAX_DEPTH - 1 {
                shallow = PyList::new(py, [shallow]).unwrap().into_any();
            }
            assert!(!scan(&module, &shallow).unwrap());
        });
    }

    #[test]
    fn the_encoder_dumps_only_a_value_that_holds_a_serializer() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let encoder = SerializerScanEncoder::new(
                eval(&module, "lambda value: ('encoded', value)").unbind(),
                eval(&module, "kinds()")
                    .cast_into::<PyDict>()
                    .unwrap()
                    .unbind(),
                module.getattr("classify").unwrap().unbind(),
                eval(&module, "lambda value: ('dumped', value)").unbind(),
            );
            let plain = eval(&module, "{'a': [1, Holder(2)]}");
            let encoded = encoder.encode(&plain).unwrap();
            assert!(encoded.get_item(1).unwrap().is(&plain));
            let secret = eval(&module, "{'a': [1, Holder(Secret())]}");
            let encoded = encoder.encode(&secret).unwrap();
            let dumped = encoded.get_item(1).unwrap();
            assert_eq!(
                dumped.get_item(0).unwrap().extract::<String>().unwrap(),
                "dumped"
            );
            assert!(dumped.get_item(1).unwrap().is(&secret));
        });
    }

    /// A JSON-like tree. `Secret` stands for a Serializer.
    #[derive(Clone, Debug)]
    enum Tree {
        Int(i64),
        Text(String),
        Secret,
        Opaque,
        List(Vec<Tree>),
        Tuple(Vec<Tree>),
        // A BTreeMap has unique keys, as a Python dict does.
        Dict(std::collections::BTreeMap<String, Tree>),
        Holder(Box<Tree>),
    }

    impl Tree {
        fn has_secret(&self) -> bool {
            match self {
                Tree::Secret => true,
                Tree::Int(_) | Tree::Text(_) | Tree::Opaque => false,
                Tree::List(items) | Tree::Tuple(items) => items.iter().any(Tree::has_secret),
                Tree::Dict(items) => items.values().any(Tree::has_secret),
                Tree::Holder(inner) => inner.has_secret(),
            }
        }

        fn build<'py>(&self, module: &Bound<'py, PyModule>) -> Bound<'py, PyAny> {
            let py = module.py();
            match self {
                Tree::Int(value) => value.into_pyobject(py).unwrap().into_any(),
                Tree::Text(value) => PyString::new(py, value).into_any(),
                Tree::Secret => module.getattr("Secret").unwrap().call0().unwrap(),
                Tree::Opaque => module.getattr("Empty").unwrap().call0().unwrap(),
                Tree::List(items) => PyList::new(py, items.iter().map(|item| item.build(module)))
                    .unwrap()
                    .into_any(),
                Tree::Tuple(items) => PyTuple::new(py, items.iter().map(|item| item.build(module)))
                    .unwrap()
                    .into_any(),
                Tree::Dict(items) => {
                    let dict = PyDict::new(py);
                    for (key, item) in items {
                        dict.set_item(key, item.build(module)).unwrap();
                    }
                    dict.into_any()
                }
                Tree::Holder(inner) => module
                    .getattr("Holder")
                    .unwrap()
                    .call1((inner.build(module),))
                    .unwrap(),
            }
        }
    }

    fn tree() -> impl Strategy<Value = Tree> {
        let leaf = prop_oneof![
            any::<i64>().prop_map(Tree::Int),
            "[a-z]{0,4}".prop_map(Tree::Text),
            Just(Tree::Opaque),
            Just(Tree::Secret),
        ];
        leaf.prop_recursive(5, 48, 5, |inner| {
            prop_oneof![
                prop::collection::vec(inner.clone(), 0..5).prop_map(Tree::List),
                prop::collection::vec(inner.clone(), 0..5).prop_map(Tree::Tuple),
                prop::collection::btree_map("[a-z]{1,3}", inner.clone(), 0..5).prop_map(Tree::Dict),
                inner.prop_map(|item| Tree::Holder(Box::new(item))),
            ]
        })
    }

    proptest! {
        #[test]
        fn finds_a_serializer_exactly_when_the_tree_holds_one(tree in tree()) {
            Python::initialize();
            Python::attach(|py| {
                let module = fixtures(py);
                let value = tree.build(&module);
                prop_assert_eq!(scan(&module, &value).unwrap(), tree.has_secret());
                Ok(())
            })?;
        }
    }
}
