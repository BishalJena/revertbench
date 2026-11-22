# Behavioral-test submission policy, version 6

`revertbench.grade.policy.check_submission(source, bad_tree)` performs static
admission checks before a test can be graded. It returns a frozen `PolicyResult`
with `allowed`, an optional `reason`, and diagnostic `details`. It never imports
the submission or the target project. Policy rejection is distinct from an
executed test failing to reproduce a regression.

Version 6 intentionally supports a restricted Python test format. A rejected
construct is not necessarily malicious; it may simply need a capability outside
this version. Cases whose reference tests require unsupported capabilities are
ineligible until the policy and its tests are deliberately extended.

Version 6 additionally permits one direct local counter callback in an ordinary
test function. It has one positional parameter and a body consisting of exactly
`nonlocal counter` followed by `counter += 1`. The counter must already be a local
integer literal binding, rather than a module value, parameter, call result or
alias of another name. This certificate is checked at the callback declaration;
it is not a proof that later rebinding or dynamic project behavior preserves the
counter's integer type. Boolean literals do not qualify. The callback cannot have
decorators, defaults, variadic/keyword-only parameters, other statements or
deeper definitions, and cannot itself be called as a submission helper. It can
be passed to a statically resolved project API. This supports a callback counting
observed deliveries without admitting a general closure or `nonlocal` grammar.

Function return annotations may be absent or the literal `None`. The callback's
parameter may also have an exact `alias.Any` annotation, where `alias` is an
unrebound `import typing` module binding. This import is annotation-only: it
grants no runtime expression, attribute, constructor or cast access. Source-tree
`typing` shadows, other typing import forms and arbitrary annotation expressions
are excluded. An optional initial `from __future__ import annotations` is the
only admitted future feature. Extraction retains this original flag when needed
by the selected annotated dependency closure, and preserves the entire original
function, including nested scope, annotations, comments and assertions. Existing
unannotated reference output is unchanged. Annotations grant no parameter types
or runtime permissions; the callback argument is not available for other
operations in the restricted counter-only body. The checker does not execute the
callback or infer how often a dynamic project API will invoke it. Existing cases
retain their original pinned policy and implementation.

Version 5 additionally excludes the repository-root `test.py` suite from project
API resolution. Direct imports, aliases and statically resolved transitive or
wildcard reexports of that exact source file are rejected, including after a
legitimate project call. The guard checks the selected resolved source path:
`src/test.py` still takes precedence and nested production modules such as
`pkg/test.py` retain their previous behavior. This matches the miner's explicit
root-suite classification without changing the accepted expression grammar.
Previously accepted cases continue to use their pinned policy and engine.

## Supported format

A file contains module-level imports, literal constants, ordinary helper
functions, and one or more functions named `test_...`. Module bindings must be
unique. Every literal-only module constant and parametrization argument is
checked for call nodes before literal evaluation; even `set()` is excluded,
because its name can be shadowed by a project function at collection time.
Function defaults, annotations, variadic arguments, keyword-only
arguments, nested definitions, classes, async functions and generators are not
supported. Test functions take no parameters except those introduced by the
literal parametrization described below. Helpers may have ordinary positional
parameters. Their unknown parameter types do not become permission to access
arbitrary attributes.

Inside functions, supported operations are assertions, literals, name assignment,
arithmetic/comparison/boolean expressions, indexing/slicing, returns, `if`, `for`,
`while`, and calls whose targets can be resolved statically. Assignment may bind
local names or unpack names; it cannot overwrite attributes or indexed objects.
Lambdas, dictionary/keyword unpacking, `try`, `raise`, custom context managers and imports inside functions are excluded.

Version 3 introduced list comprehensions inside functions of the form
`[element for name in iterable]`. It requires exactly one synchronous generator,
a simple name target, no filters and no nested comprehension. The iterable is
checked in the outer scope first; the target shadows existing names only inside
the comprehension and does not leak afterward. Its unknown value gains neither
callability nor arbitrary member access. Every element expression is checked even
when the iterable is empty. Existing calls, fix-only symbol resolution, private
member and direct metadata-key restrictions apply throughout. Known project
origin from the iterable or element is retained on the materialized list.
Generator, set and dictionary comprehensions, async clauses, multiple generators,
unpacking targets and assignment expressions remain excluded. Module constants,
parametrization and project export lists remain call-free literals. Extraction
preserves the complete original comprehension AST and its true outer dependencies;
it never rewrites the test to fit this profile. Previously accepted cases retain
their pinned policy version and engine.

A test must contain an assertion, an approved exception context or a known
project API call; a bare `pass` is not a submission.

Assertions such as `assert True` and `assert False` remain syntactically eligible.
The differential execution rules must reject them. Static admission does not
attempt to determine whether an assertion is useful or true.

For example, an eligible functional test is:

```python
from target_package import normalize


def test_normalizes_whitespace():
    assert normalize(" A ") == "a"
```

The target may instead be a statically known class constructed inside the test.
Public methods, class fields and simple `self.field` assignments in its
constructor can be resolved from source. Return annotations on project functions
can identify builtin value types or directly named project classes. A method or
attribute that cannot be resolved is rejected. Inherited members, runtime
attribute creation, string annotations and namespace packages are not resolved
by this version.

## Imports and calls

Imports may refer to public Python source modules in `bad_tree/src`, then
`bad_tree`, matching the runner's source precedence. Packages require an
`__init__.py`. A submodule is resolved within its selected parent package; root
and `src` packages are never mixed. Simple imports and transitive source reexports
are followed, with cycles and unresolved names rejected. Symlinks may not resolve
outside the bad tree. Test support modules named `tests`, `conftest` or `test_...`
and the exact root `test.py` source file are not an alternate API surface.

Project wildcard reexports can be resolved when every provider has a bounded,
direct, call-free literal list or tuple of public names in `__all__`. Visible
mutation, rebinding, ambiguous providers and collisions with explicit bindings
are rejected. This supports public package APIs without admitting wildcard
imports in the submission itself.

The external modules admitted by this version are `pytest`, `math`, and
restricted parts of `unittest` and `itertools`:

- `pytest.raises(ApprovedException, match="literal pattern")` is allowed as a
  context manager without an `as` target. It cannot expose an exception-info,
  traceback or frame object. Only builtin exception names listed in the policy
  are supported; broad termination exceptions such as `SystemExit` are excluded.
- `@pytest.mark.parametrize("name, expected", [(1, 2), ...])` is allowed on test
  functions. Its two arguments must be literal values; names must be distinct,
  and cases must be a non-empty list or tuple. Other decorators, marks, fixtures,
  plugins, hooks and `pytest` attributes are rejected.
- `math` exposes the explicitly enumerated numeric functions and constants in
  the policy. Importing it grants no access to other module attributes.
- `unittest.TestCase()` takes no arguments. Its resulting object exposes only
  `assertEqual(actual, expected)`, with exactly two positional arguments. Other
  assertions, test runners, cleanup, skipping and module attributes are
  excluded. A case tree containing a root or `src` module/package named
  `unittest` is rejected. This API supports ordinary test functions; submission
  classes remain excluded.
- `itertools.permutations(iterable)` takes exactly one positional argument and no
  keywords. Imports may use the module or the function with an alias; other
  `itertools` APIs and source-tree shadows are rejected. Its output retains known
  project origin for direct metadata checks.

Version 4 additionally permits a direct, uncaptured
`with case.subTest(label=value):` on an exact stdlib `TestCase` binding. It takes
one or two distinct named labels, with their values checked as ordinary expressions.
Positional messages, `msg`, keyword expansion, context aliases, `as` capture and
multiple contexts in the same `with` are excluded. The context alone is not an
observation. The standalone fresh instance is not run through unittest's runner:
exceptions propagate and stop the ordinary test at the first failure; successful
iterations continue. It does not produce separate subtest node IDs. Thus all
original input cases and assertions remain in the source, but a bad-tree failure
can prevent later cases from running. Base and repaired passes require all cases.

A positional expansion is admitted only as `target(*name)`, with no other
positional or keyword arguments. The target must already resolve to a project
function/class or the exact builtin `range`. The name must retain proof of a
source-built tuple/list containing one to three values in every possible shape.
Certificates are function-local: module literals keep ordinary value permissions
but never gain pack certificates, including through local aliases.
Literal case rows and loop unpacking may preserve a bounded union of shapes;
every row and expression is checked. This structural knowledge grants loop
variables no callable or member permissions. Unknown values, incompatible rows,
mutation through aliases, escaping containers, ordinary project calls and helper
calls discard proof;
a fresh literal can establish new proof. Calls/effects during literal evaluation
cannot resurrect earlier alias shapes, and effectful loops cannot reuse star
proof across iterations. The proof budget is four levels,
128 expanded shape nodes and 16 alternatives, including aliased structures.
Arbitrary runtime iterable expansion, expression operands, multiple stars and
starred calls to observation helpers, `iter`, `permutations`, mapping lookups or
submission helpers remain excluded. Existing origin and fix-only checks apply.

`iter(value)` is allowed with exactly one positional argument and no keywords;
the callable/sentinel form is excluded. Aliases retain these restrictions and
project-origin tracking. Iterator objects expose no additional methods.

Safe builtin operations are explicitly enumerated by the policy, including
`_BUILTIN_RETURNS` and `_EXCEPTIONS`. They include numeric/container constructors, `len`, ordinary
iteration helpers and simple aggregations. Public methods of known literal or
annotated string/list/tuple/dict/set values are enumerated in `_VALUE_METHODS`.
These allow ordinary behavioral assertions without permitting arbitrary method
lookup. String `format`/`format_map`, which can traverse attributes through format
fields, are deliberately absent. A callable alias is usable only when its value
is already a statically approved callable. Divergent branch/loop bindings become
unknown rather than retaining stale callable types.

## Rejected inspection and execution capabilities

The following are outside the accepted subset, including when they follow an
otherwise legitimate call that executes changed production code:

- Direct names absent from bad-tree definitions, including repair-only functions
  or attributes.
- Reflective operations such as `getattr`, `hasattr`, `vars`, `globals`, `locals`,
  `type`, dynamic import, `eval`, `exec` and compilation.
- Private attributes, reflective dunder names, source/bytecode/frame attributes,
  and the explicit version/platform/environment metadata names in the policy.
  Literal metadata keys on project-origin outputs are also rejected in subscripts
  and dictionary `get`/`pop`/`setdefault` calls. Simple aliases of those literal
  keys and lookup methods retain the restriction. Approved builtin wrappers,
  such as `dict(project_data)`, retain project origin from their arguments.
  Addition of known string constants is folded only up to 256 characters, enough
  to recognize split metadata keys without unbounded constant construction.
  This deliberately excludes
  legitimate APIs that expose those metadata names from the v1 case set; ordinary
  literal test-data dictionaries are not blanket-banned.
- File access, process creation, network operations, interpreter inspection and
  direct access to the grader through imports or builtins. Common denied modules
  include `os`, `sys`, `inspect`, `pathlib`, `subprocess`, `socket`, `coverage` and
  `revertbench`; modules outside the approved external set must resolve to
  permitted in-tree source.
- Top-level execution, conditional test collection, custom test classes,
  pytest/configuration manipulation, skip/xfail marks and arbitrary fixtures.

The adversarial tests include direct and aliased repair-only checks, metadata or
source inspection after a legitimate API call, reflection through aliases,
plugin/hook injection, test-support imports, stale branch type resolution,
source-path shadowing, cyclic reexports and source files that would perform a
side effect if imported. The last fixture proves that static checking does not
execute project startup code.

## Input and resolution bounds

Submissions are limited to 65,536 UTF-8 bytes, 3,000 AST nodes and AST depth 64.
The supported source format is UTF-8 without a BOM. An encoding cookie in either
of Python's first two recognized source lines may be omitted or name UTF-8
(case-insensitive `utf-8`, `utf8`, or `utf_8`); other spellings and encodings are
rejected before AST parsing. A raw-byte allowlist checks recognized first/second
line cookies before `tokenize` or codec-registry lookup, so unknown names cannot
invoke host-registered codec search hooks. Only then does the checker confirm the
canonical UTF-8 name, without decoding source through a caller-selected codec.
It then parses the exact UTF-8 bytes supplied to the worker, so an encoding such
as `unicode_escape` cannot transform an apparent comment into executable imports
at collection time. BOM-prefixed source is explicitly outside this version.
Malformed Python, invalid Unicode and excess complexity are rejected without
execution. Source lookup is limited to 64 modules of at most 1 MiB each. Exceeding
these bounds is unsupported, rather than a reason to import a module to discover
its runtime shape.

`project_unavailable` indicates missing or inaccessible grading infrastructure;
it must not be scored as a model failure. Other policy reasons describe the
unsupported submission or source-resolution requirement. Execution and case
validation remain responsible for identifying build/environment failures.

## Bounded soundness argument

The static checker grants only the names, syntax and calls above. It rejects
known direct inspection and runner-manipulation patterns before execution and
requires resolvable project names to exist on the bad tree. Following source
imports and definitions rather than runtime imports avoids executing the code
being inspected. Restricted literal parametrization avoids arbitrary collection
code disguised as decorator arguments.

Those properties do **not** prove that arbitrary Python is safe, that an
assertion detects the intended regression, or that every accepted attribute
exists for every runtime input. Project APIs can themselves perform side
effects, return unexpected objects, implement overloaded operations, or expose
information indirectly. Metadata accessed through general computed keys,
custom wrappers, helper parameters or indirect API outputs is not completely
tracked; the direct
literal-key checks are not a general information-flow analysis. Python's dynamic
object model, decorators and native extensions can disagree with source-level
approximations. Project code can indirectly mutate export lists, standard-library
objects or import state; source-visible export checks and module-shadowing guards
do not prove arbitrary import semantics. The tests exercise
known counterexamples; they are not a completeness proof for this language.

The policy's approved exception names apply to caught exceptions in
`pytest.raises`. They do not make every such exception eligible as an uncaught
bad-tree failure: execution grading separately checks the failure type, phase
and changed-source provenance.

The complete grader must independently require the same actual tests on both
trees, meaningful bad-tree failures, passes on the controlled repair, changed
line coverage from each failing test's call phase, and stable repeated outcomes.
Coverage demonstrates execution, not causation: an unrelated assertion in the
same test can still execute changed lines. Explicit review of reference tests and
controlled repair construction establish case relevance; grading generated
tests remains evidence under the stated limitations rather than a semantic
proof.

The initial local subprocess backend is for trusted toy fixtures only. Static
admission is not an isolation boundary. Generated submissions need an isolated
worker with controlled files, configuration, resource limits and networking.
Reports produced in the pytest interpreter are also not universally authentic
against arbitrary in-process manipulation. The orchestrator must validate
execution evidence and report these residual limitations explicitly.


Qualified unittest extraction permits unrelated inert helper definitions only
with ordinary positional parameters and call-free literal defaults. They cannot
participate in the selected test's dependency closure. The complete trusted
TestCase class/instance namespace, including internal methods and state, remains
reserved against overrides; class state, decorators, annotations, inherited or
rebound bases, fixture hooks and selected-method helper/property/state access
remain excluded. The selected body may directly use only `assertEqual` and the
supported `subTest` context, and is preserved without assertion/input rewriting.
