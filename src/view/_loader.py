from __future__ import annotations

import os
import runpy
import warnings
from dataclasses import _MISSING_TYPE, Field, dataclass
from pathlib import Path

try:
    from types import UnionType
except ImportError:
    UnionType = None
from typing import (TYPE_CHECKING, ForwardRef, Iterable, NamedTuple, TypedDict,
                    Union, get_args, get_type_hints)

try:
    from pydantic.fields import ModelField
except ImportError:
    from pydantic.fields import FieldInfo as ModelField

if not TYPE_CHECKING:
    from typing import _eval_type
else:

    def _eval_type(*args) -> Any:
        ...


from ._logging import Internal
from ._util import set_load
from .exceptions import InvalidBodyError, LoaderWarning
from .routing import BodyParam, Method, Route, RouteInput, _NoDefault
from .typing import Any, RouteInputDict, TypeInfo, ValueType

ExtNotRequired = None
try:
    from typing import NotRequired
except ImportError:
    NotRequired = None
    from typing_extensions import NotRequired as ExtNotRequired

from typing_extensions import Annotated, TypeGuard

_NOT_REQUIRED_TYPES = []

if ExtNotRequired:
    _NOT_REQUIRED_TYPES.append(ExtNotRequired)

if NotRequired:
    _NOT_REQUIRED_TYPES.append(NotRequired)

if TYPE_CHECKING:
    from .app import App as ViewApp

    _TypedDictMeta = None
else:
    from typing import _TypedDictMeta

__all__ = "load_fs", "load_simple", "finalize"

TypingUnionType = type(Union[str, int])

TYPECODE_ANY = 0
TYPECODE_STR = 1
TYPECODE_INT = 2
TYPECODE_BOOL = 3
TYPECODE_FLOAT = 4
TYPECODE_DICT = 5
TYPECODE_NONE = 6
TYPECODE_CLASS = 7
TYPECODE_CLASSTYPES = 8


_BASIC_CODES = {
    str: TYPECODE_STR,
    int: TYPECODE_INT,
    bool: TYPECODE_BOOL,
    float: TYPECODE_FLOAT,
    dict: TYPECODE_DICT,
    None: TYPECODE_NONE,
    Any: TYPECODE_ANY,
}

"""
Type info should contain three things:
    - Type Code
    - Type Object (only set when using a __view_body__ object)
    - Children (i.e. the `int` part of dict[str, int])
    - Default (only set when typecode is TYPECODE_CLASSTYPES)

This can be formatted as so:
    [(union1_tc, None, []), (union2_tc, None, [(type_tc, obj, [])])]
"""


class _ViewNotRequired:
    __VIEW_NOREQ__ = 1


def _format_body(
    vbody_types: dict,
    doc: dict[Any, LoaderDoc],
    origin: type[Any],
    *,
    not_required: set[str] | None = None,
) -> list[TypeInfo]:
    """Generate a type info list from view body types."""
    not_required = not_required or set()
    if not isinstance(vbody_types, dict):
        raise InvalidBodyError(
            f"__view_body__ should return a dict, not {type(vbody_types)}",  # noqa
        )

    vbody_final = {}
    vbody_defaults = {}

    for k, raw_v in vbody_types.items():
        if not isinstance(k, str):
            raise InvalidBodyError(
                f"all keys returned by __view_body__ should be strings, not {type(k)}"  # noqa
            )

        default = _NoDefault
        v = raw_v.types if isinstance(raw_v, BodyParam) else raw_v

        if isinstance(v, str):
            scope = getattr(origin, "_view_scope", globals())
            v = _eval_type(ForwardRef(v), scope, scope)

        if isinstance(raw_v, BodyParam):
            default = raw_v.default

        if (getattr(raw_v, "__origin__", None) in _NOT_REQUIRED_TYPES) or (
            k in not_required
        ):
            v = get_args(raw_v)
            default = _ViewNotRequired
        iter_v = v if isinstance(v, (tuple, list)) else (v,)
        vbody_final[k] = _build_type_codes(
            iter_v,
            doc,
            key_name=k,
            default=default,
        )
        vbody_defaults[k] = default

    return [
        (TYPECODE_CLASSTYPES, k, v, vbody_defaults[k])
        for k, v in vbody_final.items()
    ]


AnnotatedType = type(Annotated[str, ""])


def is_annotated(hint: Any) -> TypeGuard[AnnotatedType]:
    return (type(hint) is AnnotatedType) and hasattr(hint, "__metadata__")


@dataclass
class LoaderDoc:
    desc: str
    tp: Any
    default: Any


class _NotSet:
    """Sentinel value for default being not set in _build_type_codes."""

    ...


def _build_type_codes(
    inp: Iterable[type[ValueType]],
    doc: dict[Any, LoaderDoc] | None = None,
    *,
    key_name: str | None = None,
    default: Any | _NoDefault = _NotSet,
) -> list[TypeInfo]:
    """Generate types from a list of types.

    Args:
        inp: Iterable containing each type.
        doc: Auto-doc dictionary when a docstring is extracted.
        key_name: Name of the current key. Only needed for auto-doc purposes.
        default: Default value. Only needed for auto-doc purposes."""
    if not inp:
        return []

    codes: list[TypeInfo] = []

    for tp in inp:
        if is_annotated(tp):
            if doc is None:
                raise TypeError(f"Annotated is not valid here ({tp})")

            if not key_name:
                raise RuntimeError("internal error: key_name is None")

            if default is _NotSet:
                raise RuntimeError("internal error: default is _NotSet")

            tmp = tp.__origin__
            doc[key_name] = LoaderDoc(tp.__metadata__[0], tmp, default)
            tp = tmp
        elif doc is not None:
            if not key_name:
                raise RuntimeError("internal error: key_name is None")

            if default is _NotSet:
                raise RuntimeError("internal error: default is _NotSet")

            doc[key_name] = LoaderDoc("No description provided.", tp, default)

        type_code = _BASIC_CODES.get(tp)

        if type_code:
            codes.append((type_code, None, []))
            continue

        if (TypedDict in getattr(tp, "__orig_bases__", [])) or (
            type(tp) == _TypedDictMeta
        ):
            try:
                body = get_type_hints(tp)
            except KeyError:
                body = tp.__annotations__

            opt = getattr(tp, "__optional_keys__", None)

            class _Transport:
                @staticmethod
                def __view_construct__(**kwargs):
                    return kwargs

            doc = {}
            codes.append(
                (
                    TYPECODE_CLASS,
                    _Transport,
                    _format_body(body, doc, tp, not_required=opt),
                ),
            )
            setattr(tp, "_view_doc", doc)
            continue

        if (NamedTuple in getattr(tp, "__orig_bases__", [])) or (
            hasattr(tp, "_field_defaults")
        ):
            defaults = tp._field_defaults  # type: ignore
            tps = {}
            try:
                hints = get_type_hints(tp)
            except KeyError:
                hints = getattr(tp, "_field_types", tp.__annotations__)

            for k, v in hints.items():
                if k in defaults:
                    tps[k] = BodyParam(v, defaults[k])
                else:
                    tps[k] = v

            doc = {}
            codes.append((TYPECODE_CLASS, tp, _format_body(tps, doc, tp)))
            setattr(tp, "_view_doc", doc)
            continue

        dataclass_fields: dict[str, Field] | None = getattr(
            tp, "__dataclass_fields__", None
        )

        if dataclass_fields:
            tps = {}
            for k, v in dataclass_fields.items():
                if isinstance(v.default, _MISSING_TYPE) and (
                    isinstance(v.default_factory, _MISSING_TYPE)
                ):
                    tps[k] = v.type
                else:
                    default = (
                        v.default
                        if not isinstance(v.default, _MISSING_TYPE)
                        else v.default_factory
                    )
                    tps[k] = BodyParam(v.type, default)

            doc = {}
            codes.append((TYPECODE_CLASS, tp, _format_body(tps, doc, tp)))
            setattr(tp, "_view_doc", doc)
            continue

        pydantic_fields: dict[str, ModelField] | None = getattr(
            tp, "__fields__", None
        ) or getattr(tp, "model_fields", None)
        if pydantic_fields:
            tps = {}

            for k, v in pydantic_fields.items():
                if (not v.default) and (not v.default_factory):
                    tps[k] = v.outer_type_
                else:
                    tps[k] = BodyParam(
                        v.outer_type_,
                        v.default or v.default_factory,
                    )

            doc = {}
            codes.append((TYPECODE_CLASS, tp, _format_body(tps, doc, tp)))
            setattr(tp, "_view_doc", doc)
            continue

        vbody = getattr(tp, "__view_body__", None)
        if vbody:
            if callable(vbody):
                vbody_types = vbody()
            else:
                vbody_types = vbody

            doc = {}
            codes.append(
                (TYPECODE_CLASS, tp, _format_body(vbody_types, doc, tp))
            )
            setattr(tp, "_view_doc", doc)
            continue

        origin = getattr(tp, "__origin__", None)  # typing.GenericAlias

        if (type(tp) in {UnionType, TypingUnionType}) and (origin is not dict):
            new_codes = _build_type_codes(get_args(tp))
            codes.extend(new_codes)
            continue

        if origin is not dict:
            raise InvalidBodyError(f"{tp} is not a valid type for routes")

        key, value = get_args(tp)

        if key is not str:
            raise InvalidBodyError(
                f"dictionary keys must be strings, not {key}"
            )

        value_args = get_args(value)

        if not len(value_args):
            value_args = (value,)

        tp_codes = _build_type_codes(value_args)
        codes.append((TYPECODE_DICT, None, tp_codes))

    return codes


def _format_inputs(inputs: list[RouteInput]) -> list[RouteInputDict]:
    """Convert a list of route inputs to a proper dictionary that the C loader can handle.
    This function also will generate the typecodes for the input."""
    result: list[RouteInputDict] = []

    for i in inputs:
        type_codes = _build_type_codes(i.tp)
        result.append(
            {
                "name": i.name,
                "type_codes": type_codes,
                "default": i.default,  # type: ignore
                "validators": i.validators,
                "is_body": i.is_body,
                "has_default": i.default is not _NoDefault,
            }
        )

    return result


def finalize(routes: list[Route], app: ViewApp):
    """Attach list of routes to an app and validate all parameters.

    Args:
        routes: List of routes.
        app: App to attach to.
    """
    virtual_routes: dict[str, list[Route]] = {}

    targets = {
        Method.GET: app._get,
        Method.PATCH: app._post,
        Method.PUT: app._put,
        Method.PATCH: app._patch,
        Method.DELETE: app._delete,
        Method.OPTIONS: app._options,
    }

    for route in routes:
        set_load(route)
        target = targets[route.method]

        if (not route.path) and (not route.parts):
            raise TypeError("route did not specify a path")
        lst = virtual_routes.get(route.path or "")

        if lst:
            if route.method in [i.method for i in lst]:
                raise ValueError(
                    f"duplicate route: {route.method.name} for {route.path}",
                )
            lst.append(route)
        else:
            virtual_routes[route.path or ""] = [route]

        app.loaded_routes.append(route)
        target(
            route.path,  # type: ignore
            route.func,
            route.cache_rate,
            _format_inputs(route.inputs),
            route.errors or {},
            route.parts,  # type: ignore
        )


def load_fs(app: ViewApp, target_dir: Path) -> None:
    """Filesystem loading implementation.
    Similiar to NextJS's routing system. You take `target_dir` and search it,
    if a file is found and not prefixed with _, then convert the directory structure
    to a path. For example, target_dir/hello/world/index.py would be converted to a
    route for /hello/world

    Args:
        app: App to attach routes to.
        target_dir: Directory to search for routes.
    """
    Internal.info("loading using filesystem")
    Internal.debug(f"loading {app}")

    routes: list[Route] = []

    for root, _, files in os.walk(target_dir):
        for f in files:
            if f.startswith("_"):
                continue

            path = os.path.join(root, f)
            Internal.info(f"loading: {path}")
            mod = runpy.run_path(path)
            current_routes: list[Route] = []

            for i in mod.values():
                if isinstance(i, Route):
                    if i.method in [x.method for x in current_routes]:
                        warnings.warn(
                            "same method used twice during filesystem loading",
                            LoaderWarning,
                        )
                    current_routes.append(i)

            if not current_routes:
                raise ValueError(f"{path} has no set routes")

            for x in current_routes:
                if x.path:
                    warnings.warn(
                        f"path was passed for {x} when filesystem loading is enabled"  # noqa
                    )
                else:
                    path_obj = Path(path)
                    stripped = list(
                        path_obj.parts[len(target_dir.parts) :]
                    )  # noqa
                    if stripped[-1] == "index.py":
                        stripped.pop(len(stripped) - 1)

                    stripped_obj = Path(*stripped)
                    stripped_path = str(stripped_obj).rsplit(
                        ".",
                        maxsplit=1,
                    )[0]
                    x.path = "/" + stripped_path

            for x in current_routes:
                routes.append(x)

    finalize(routes, app)


def load_simple(app: ViewApp, target_dir: Path) -> None:
    """Simple loading implementation.
    Simple loading is essentially searching a directory recursively
    for files, and then extracting Route instances from each file.

    If a file is prefixed with _, it will not be loaded.

    Args:
        app: App to attach routes to.
        target_dir: Directory to search for routes.

    """
    Internal.info("loading using simple strategy")
    routes: list[Route] = []

    for root, _, files in os.walk(target_dir):
        for f in files:
            if f.startswith("_"):
                continue

            path = os.path.join(root, f)
            Internal.info(f"loading: {path}")
            mod = runpy.run_path(path)
            mini_routes: list[Route] = []

            for i in mod.values():
                if isinstance(i, Route):
                    mini_routes.append(i)

            for route in mini_routes:
                if not route.path:
                    raise ValueError(
                        "omitting path is only supported"
                        " on filesystem loading",
                    )

                routes.append(route)

    finalize(routes, app)
