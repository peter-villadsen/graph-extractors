"""Best-effort ``CALLS`` resolution for Python that never guesses.

Python is dynamic, so a call can only be tied to a definition when the code
leaves no doubt. The rule here is that a missing edge is acceptable but a
wrong one is not: every rule below declines rather than guesses.

It works in two passes over a run:

1. ``CallResolver.add_module`` analyses each module's AST right after the
   ``GraphBuilder`` has built it. It records a light scope tree (module,
   class and function scopes with every name binding in them) and the
   module's call sites, using the builder's AST-node -> graph-node-id map.
   The AST itself is not kept.
2. ``CallResolver.relationships`` resolves every recorded call site once all
   modules are known, so calls through imports reach other files, and
   returns one ``CALLS`` relationship per distinct (caller, callee) pair
   with a ``count``.

A name resolves only when its scope binds it **exactly once** and that
binding is a ``def``, ``class`` or ``import`` placed directly in the scope's
body. Any other binding of the name (an assignment, a parameter, a loop
target, a ``def`` inside an ``if``, a ``global`` rebinding from another
function, ...) makes it unresolvable. Bindings are collected generously, for
example names bound in a nested lambda or comprehension count as bindings
of the enclosing function; over-counting can only drop edges, never add a
wrong one.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from .cypher import Relationship

_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)

# Decorators that leave a function callable as itself.
_TRANSPARENT_DECORATORS = frozenset({"staticmethod", "classmethod"})


@dataclass
class Binding:
    """One way a name is bound in a scope.

    ``kind`` is one of:

    * ``def`` / ``class``: a definition directly in the scope's body
      (``node_id`` is its graph node; ``scope`` is a class's body scope);
    * ``from``: ``from module import name`` directly in the body
      (``target`` = ``(absolute module, name)``);
    * ``module``: ``import a.b as m`` directly in the body (``target`` = ``'a.b'``);
    * ``package``: ``import a.b`` directly in the body, which binds ``a``
      (``target`` = the set of dotted modules imported this way);
    * ``param`` or ``other``: never resolved.
    """

    kind: str
    lineno: int = 0
    node_id: str | None = None
    target: object = None
    # Plain-name decorators of a def/class; None if any decorator is more
    # than a plain name.
    decorators: tuple | None = ()
    # A class whose ``class`` line has keywords (a metaclass, ...).
    has_class_keywords: bool = False
    scope: Scope | None = None
    # The module a def is in, to check its decorator names aren't rebound.
    module: ModuleInfo | None = None


@dataclass
class ModuleInfo:
    """A module in the run: its dotted name, the package its relative
    imports resolve against, and its top-level scope."""

    name: str
    package: str
    scope: Scope | None = None
    has_star_import: bool = False


@dataclass(eq=False)
class Scope:
    """A module, class or function body and the names bound in it."""

    kind: str  # 'module' | 'class' | 'function'
    node_id: str
    parent: Scope | None
    module: ModuleInfo
    bindings: dict = field(default_factory=dict)
    globals: set = field(default_factory=set)
    nonlocals: set = field(default_factory=set)
    # For a method: the name of its first parameter (self / cls), unless it
    # is a staticmethod. For a class: the binding that defines it.
    self_name: str | None = None
    class_binding: Binding | None = None

    def bind(self, name, binding):
        """Record one more binding of ``name`` in this scope."""
        self.bindings.setdefault(name, []).append(binding)


@dataclass
class CallSite:
    """A call whose callee is a plain or dotted name, e.g. ``('f',)``,
    ``('self', 'm')`` or ``('pkg', 'mod', 'f')``."""

    caller: Scope
    lineno: int
    parts: tuple


def package_of(module_name, is_package):
    """The package that relative imports in ``module_name`` resolve against:
    the module itself for a package's ``__init__``, else its parent
    (``''`` for a top-level module)."""
    if is_package:
        return module_name
    return module_name.rpartition(".")[0]


def absolute_module(package, level, module):
    """Resolve ``from <level dots><module> import ...`` written in a module
    of ``package`` to an absolute module name, or ``None`` if it climbs out
    of the top-level package."""
    if level == 0:
        return module
    parts = package.split(".") if package else []
    drop = level - 1
    if drop >= len(parts):
        return None
    base = ".".join(parts[: len(parts) - drop])
    return f"{base}.{module}" if module else base


def call_parts(func):
    """The dotted name a call's ``func`` expression spells, or ``None`` for
    anything else (a call result, a subscript, ...)."""
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return None
    parts.append(func.id)
    return tuple(reversed(parts))


class _ModuleAnalyzer:
    """Builds the scope tree of one module and collects its call sites and
    attribute stores into the resolver."""

    def __init__(self, info, node_ids, resolver):
        self._info = info
        self._node_ids = node_ids
        self._resolver = resolver
        self._function_scopes = []

    def analyze(self, tree):
        """Analyse a parsed module."""
        scope = Scope("module", self._node_ids[id(tree)], None, self._info)
        self._info.scope = scope
        self._walk_body(tree.body, scope)
        self._apply_global_and_nonlocal_rebinding()

    def _walk_body(self, statements, scope):
        for statement in statements:
            self._walk(statement, scope, direct=True)

    def _walk(self, node, scope, direct=False):
        """Visit ``node`` as part of ``scope``. ``direct`` is true for a
        statement that sits directly in the scope's body."""
        if isinstance(node, _FUNCTION_TYPES):
            self._walk_function(node, scope, direct)
            return
        if isinstance(node, ast.ClassDef):
            self._walk_class(node, scope, direct)
            return
        if isinstance(node, ast.Import):
            self._bind_import(node, scope, direct)
        elif isinstance(node, ast.ImportFrom):
            self._bind_import_from(node, scope, direct)
        elif isinstance(node, ast.Global):
            scope.globals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            scope.nonlocals.update(node.names)
        elif isinstance(node, ast.Call):
            self._record_call(node, scope)
        else:
            self._bind_other(node, scope)
        for child in ast.iter_child_nodes(node):
            self._walk(child, scope)

    def _walk_function(self, node, scope, direct):
        """A def: its decorators and defaults run in ``scope``; its body is a
        new function scope."""
        for expression in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if expression is not None:
                self._walk(expression, scope)
        node_id = self._node_ids[id(node)]
        decorators = _decorator_names(node)
        scope.bind(
            node.name,
            Binding(
                "def" if direct else "other",
                node.lineno,
                node_id,
                decorators=decorators,
                module=self._info,
            ),
        )
        function = Scope("function", node_id, scope, self._info)
        self._function_scopes.append(function)
        arguments = node.args
        for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
            function.bind(arg.arg, Binding("param", node.lineno))
        for arg in (arguments.vararg, arguments.kwarg):
            if arg is not None:
                function.bind(arg.arg, Binding("param", node.lineno))
        if scope.kind == "class" and direct and "staticmethod" not in (decorators or ()):
            positional = [*arguments.posonlyargs, *arguments.args]
            if positional:
                function.self_name = positional[0].arg
        self._walk_body(node.body, function)

    def _walk_class(self, node, scope, direct):
        """A class: its decorators, bases and keywords run in ``scope``; its
        body is a new class scope."""
        for expression in [*node.decorator_list, *node.bases, *node.keywords]:
            self._walk(expression, scope)
        binding = Binding(
            "class" if direct else "other",
            node.lineno,
            self._node_ids[id(node)],
            decorators=_decorator_names(node),
            has_class_keywords=bool(node.keywords),
        )
        body = Scope("class", binding.node_id, scope, self._info, class_binding=binding)
        binding.scope = body
        scope.bind(node.name, binding)
        self._walk_body(node.body, body)

    def _bind_import(self, node, scope, direct):
        """``import a.b as m`` binds ``m`` to module ``a.b``; ``import a.b``
        binds ``a``, through which ``a.b`` is reachable."""
        for alias in node.names:
            if alias.asname:
                kind, name, target = "module", alias.asname, alias.name
            else:
                kind, name, target = "package", alias.name.partition(".")[0], frozenset({alias.name})
            scope.bind(name, Binding(kind if direct else "other", node.lineno, target=target))

    def _bind_import_from(self, node, scope, direct):
        """``from module import name [as alias]``; a relative module is made
        absolute here, and a star import disables bare-name resolution."""
        module = absolute_module(self._info.package, node.level, node.module)
        for alias in node.names:
            if alias.name == "*":
                self._info.has_star_import = True
                continue
            resolvable = direct and module is not None
            binding = Binding(
                "from" if resolvable else "other", node.lineno, target=(module, alias.name)
            )
            scope.bind(alias.asname or alias.name, binding)

    def _bind_other(self, node, scope):
        """Every other construct that binds a name, and every attribute
        store (which could replace a method)."""
        name = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            name = node.id
        elif isinstance(node, ast.arg):  # lambda parameters
            name = node.arg
        elif isinstance(node, ast.ExceptHandler):
            name = node.name
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            name = node.name
        elif isinstance(node, ast.MatchMapping):
            name = node.rest
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            self._resolver.note_attribute_store(node.attr)
        if name:
            scope.bind(name, Binding("other", getattr(node, "lineno", 0)))

    def _record_call(self, node, scope):
        """Record a call with a (dotted) name as callee, and note ``setattr``
        calls, which can replace methods."""
        parts = call_parts(node.func)
        if parts == ("setattr",):
            name = node.args[1] if len(node.args) > 1 else None
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                self._resolver.note_attribute_store(name.value)
            else:
                self._resolver.note_attribute_store(None)
        if parts is not None:
            self._resolver.record_call(CallSite(scope, node.lineno, parts))

    def _apply_global_and_nonlocal_rebinding(self):
        """A function that assigns a ``global`` name rebinds it in the module;
        one that assigns a ``nonlocal`` name rebinds it in every enclosing
        function. Either makes that name unresolvable there."""
        for function in self._function_scopes:
            for name in function.globals & function.bindings.keys():
                self._info.scope.bind(name, Binding("other"))
            for name in function.nonlocals & function.bindings.keys():
                enclosing = function.parent
                while enclosing is not None and enclosing.kind != "module":
                    if enclosing.kind == "function":
                        enclosing.bind(name, Binding("other"))
                    enclosing = enclosing.parent


def _decorator_names(node):
    """The names of a def/class's decorators, or ``None`` if any decorator
    is not a plain name (``@app.route('/')``, ``@cache(1)``, ...)."""
    names = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Name):
            return None
        names.append(decorator.id)
    return tuple(names)


class CallResolver:
    """Run-wide, two-pass resolution of call sites to definitions.

    Resolved call forms (anything else gets no edge):

    * ``f()``: ``f`` is a ``def`` in the calling scope or an enclosing
      function or module scope, found by Python's own scoping rules;
    * ``C()``: ``C`` is such a class and its own body defines ``__init__``
      (-> ``C.__init__``);
    * ``self.m()`` / ``cls.m()``: in a method of class ``C`` (through its first
      parameter), where ``C``'s own body defines ``m`` (-> ``C.m``);
    * ``C.m()``: ``C`` resolves to a class whose own body defines ``m``;
    * ``f()`` / ``C()`` / ``C.m()`` after ``from pkg.mod import f`` (or ``as g``,
      relative imports included), where ``pkg.mod`` is in the run;
    * ``mod.f()`` after ``import pkg.mod as mod``, and ``pkg.mod.f()`` after
      ``import pkg.mod``, where ``pkg.mod`` is in the run.

    A target function or method must be undecorated (``staticmethod`` and
    ``classmethod`` excepted), and a target class undecorated and without
    keywords such as ``metaclass=``. A method, ``__init__`` included, is not
    resolved once any attribute of that name is assigned anywhere in the run
    (``obj.m = ...``, ``setattr(obj, 'm', ...)``), nor at all after a
    ``setattr`` with a computed name.
    """

    def __init__(self):
        # module name -> ModuleInfo, or None when two files share the name
        self._modules = {}
        self._calls = []
        self._stored_attributes = set()
        self._dynamic_setattr = False

    # -- pass 1: collection ------------------------------------------------

    def add_module(self, module_name, is_package, tree, node_ids):
        """Analyse one module of the run. ``node_ids`` maps ``id(ast node)``
        to graph node id, as built by ``GraphBuilder``."""
        info = ModuleInfo(module_name, package_of(module_name, is_package))
        self._modules[module_name] = None if module_name in self._modules else info
        _ModuleAnalyzer(info, node_ids, self).analyze(tree)

    def record_call(self, call):
        """Record a pending call site."""
        self._calls.append(call)

    def note_attribute_store(self, name):
        """Record that an attribute named ``name`` is assigned somewhere in the
        run (``None``: by a ``setattr`` whose name isn't a literal)."""
        if name is None:
            self._dynamic_setattr = True
        else:
            self._stored_attributes.add(name)

    # -- pass 2: resolution ------------------------------------------------

    def relationships(self):
        """Resolve every recorded call site and return one ``CALLS``
        relationship per distinct (caller, callee) pair, in first-seen
        order, with the number of call sites as ``count``."""
        counts = {}
        for call in self._calls:
            target = self._resolve(call)
            if target is not None:
                key = (call.caller.node_id, target)
                counts[key] = counts.get(key, 0) + 1
        return [
            Relationship(caller, "CALLS", callee, {"count": count})
            for (caller, callee), count in counts.items()
        ]

    def _resolve(self, call):
        """The graph node id of the definition ``call`` reaches, or ``None``."""
        if len(call.parts) == 1:
            binding = self._lookup(call.caller, call.parts[0], call.lineno)
            return self._call_target(binding)
        if len(call.parts) == 2 and self._is_self_call(call):
            return self._method(call.caller.parent, call.parts[1])
        return self._attribute_call_target(call)

    def _lookup(self, scope, name, lineno):
        """The single binding ``name`` refers to from ``scope`` under Python's
        scoping rules (class bodies are only visible to their own code), or
        ``None`` if it is unbound here (a builtin, say) or ambiguous."""
        current = scope
        while current is not None:
            if current.kind == "class" and current is not scope:
                current = current.parent
                continue
            if name in current.globals:
                current = current.module.scope
            elif name in current.nonlocals:
                current = current.parent
                continue
            if name in current.bindings:
                if current.kind == "module" and current.module.has_star_import:
                    return None
                binding = _single(current.bindings[name])
                if binding is not None and current is scope and binding.lineno > lineno:
                    return None  # called before it is bound
                return binding
            if current.kind == "module":
                return None
            current = current.parent
        return None

    def _is_self_call(self, call):
        """``self.m()`` in a method, through its unrebound first parameter."""
        caller = call.caller
        head = call.parts[0]
        return (
            caller.kind == "function"
            and caller.self_name == head
            and head not in caller.globals | caller.nonlocals
            and len(caller.bindings.get(head, [])) == 1
        )

    def _attribute_call_target(self, call):
        """``C.m()``, ``mod.f()`` and ``pkg.mod.f()`` (see the class docstring)."""
        head, *middle, last = call.parts
        binding = self._lookup(call.caller, head, call.lineno)
        if binding is None:
            return None
        if binding.kind == "from" and not middle:
            binding = self._export(*binding.target)
            if binding is None:
                return None
        if binding.kind == "class" and not middle:
            return self._method(binding.scope, last)
        if binding.kind == "module" and not middle:
            return self._module_function(binding.target, last)
        if binding.kind == "package":
            module = ".".join([head, *middle])
            if module in binding.target:
                return self._module_function(module, last)
        return None

    def _call_target(self, binding):
        """What calling the name bound by ``binding`` runs: a function, or a
        class's ``__init__``."""
        if binding is None:
            return None
        if binding.kind == "from":
            binding = self._export(*binding.target)
            if binding is None:
                return None
        if binding.kind == "def":
            return binding.node_id if self._decorators_ok(binding) else None
        if binding.kind == "class":
            return self._constructor(binding.scope)
        return None

    def _constructor(self, class_scope):
        """``C()`` reaches ``C.__init__``, unless ``__new__`` could intervene."""
        if "__new__" in class_scope.bindings:
            return None
        return self._method(class_scope, "__init__")

    def _method(self, class_scope, name):
        """The method ``name`` that class ``class_scope`` defines itself."""
        if class_scope is None or class_scope.kind != "class":
            return None
        defining = class_scope.class_binding
        if defining.decorators != () or defining.has_class_keywords:
            return None
        if self._dynamic_setattr or name in self._stored_attributes:
            return None
        binding = _single(class_scope.bindings.get(name, []))
        if binding is None or binding.kind != "def" or not self._decorators_ok(binding):
            return None
        return binding.node_id

    def _module_function(self, module_name, name):
        """Calling ``name`` as an attribute of module ``module_name``."""
        return self._call_target(self._export(module_name, name))

    def _export(self, module_name, name):
        """The ``def``/``class`` binding that module ``module_name`` (in the
        run, unambiguous, without star imports) gives ``name``, if any."""
        info = self._modules.get(module_name)
        if info is None or info.has_star_import:
            return None
        binding = _single(info.scope.bindings.get(name, []))
        if binding is None or binding.kind not in ("def", "class"):
            return None
        return binding

    def _decorators_ok(self, binding):
        """No decorators, or only the builtin ``staticmethod``/``classmethod``
        (not rebound in the def's module, which could do so by star import)."""
        if binding.decorators == ():
            return True
        if binding.decorators is None or not set(binding.decorators) <= _TRANSPARENT_DECORATORS:
            return False
        module = binding.module
        rebound = set(binding.decorators) & module.scope.bindings.keys()
        return not rebound and not module.has_star_import


def _single(bindings):
    """The one binding in ``bindings``. Several ``import a.x`` / ``import a.y``
    statements all bind ``a`` to the same package, so they merge."""
    if len(bindings) == 1:
        return bindings[0]
    if bindings and all(b.kind == "package" for b in bindings):
        targets = frozenset().union(*(b.target for b in bindings))
        return Binding("package", min(b.lineno for b in bindings), target=targets)
    return None
