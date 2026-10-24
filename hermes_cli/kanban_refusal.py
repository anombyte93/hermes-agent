"""The protected-verb refusal: one token-free message for every parse failure.

``hermes kanban block|schedule|promote|request-changes`` calls carry reason
text, and a reason can carry a secret. argparse's own error messages quote
caller words (``unrecognized arguments: --X``, ``ignored explicit argument
'X'``), so on a protected-verb call EVERY parse failure, at any level of the
parser tree, is replaced by ``invalid_arguments_refusal(verb)``, exit code 2
(reviews r4 and r5 of #133).

Kept free of heavy imports: the light chat parser (``main._light_chat_parser``)
builds with these classes on the fast startup path.
"""

from __future__ import annotations

import argparse
import contextvars
import functools
import sys
from typing import Optional

# The four kanban verbs whose calls never echo a caller word on a parse failure.
PROTECTED_VERBS = frozenset({"block", "schedule", "promote", "request-changes"})
_PROTECTED_VERBS = PROTECTED_VERBS


def is_protected_verb_call(argv) -> bool:
    """A ``hermes`` argv is a protected-verb call when it holds the word
    ``kanban`` and one of the four protected verbs."""
    words = [word for word in argv if isinstance(word, str)]
    return "kanban" in words and any(word in PROTECTED_VERBS for word in words)


# Set while a kanban parse whose argv names a protected verb runs; holds the
# verb named in the refusal. One context-local value covers every parser of the
# tree (kanban, the leaf, any nested group) for the duration of that parse.
_REFUSING_AS: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "kanban_refusing_as", default=None)


def invalid_arguments_refusal(verb: str) -> str:
    """The ONE message for every argparse failure on a protected-verb call.

    ``verb`` is a subcommand name from the parser's own table, never caller
    text, so the message carries no word the caller supplied.
    """
    return f"kanban {verb}: invalid arguments; see --help"


def _verb_table(parser: argparse.ArgumentParser) -> set:
    """Every subcommand name ``parser`` dispatches to (its own table, fixed text)."""
    return {name for action in parser._actions
            if isinstance(action, argparse._SubParsersAction) for name in action.choices}


def _resolved_verb(parser: argparse.ArgumentParser, args: list) -> str:
    """The subcommand the call names (first argv word in the verb table), else
    the first protected verb in argv. Always a fixed name, never caller text."""
    verbs = _verb_table(parser)
    named = [word for word in args if isinstance(word, str) and word in verbs]
    if named:
        return named[0]
    return next(word for word in args if isinstance(word, str) and word in _PROTECTED_VERBS)


class _ProtectedVerbRefusal:
    """Mixin for every parser that can see a protected-verb call: the one
    refusal choke point.

    While a protected-verb parse is armed, ANY argparse failure, at ANY level
    of the parser tree, becomes ``invalid_arguments_refusal`` with exit code 2
    (an ``ArgumentError`` carrying it when ``exit_on_error`` is off, as in
    ``run_slash``): an ``ArgumentError`` such as a surplus ``--help=VALUE`` or a
    bad ``--ids`` word, leftovers after the verb, and leftovers at any parent
    level before it. argparse's own messages quote caller words, and a reason
    can carry a secret (reviews r4 and r5 of #133). Every failure path ends in
    ``error()``, or in ``parse_known_args`` returning leftovers or raising, so
    those two methods are the whole choke point.

    Which parse arms the refusal is the only thing the subclasses decide
    (``_arming_verb``): this base never arms on its own, it only refuses while
    an enclosing parse is armed. It is the class of every ``hermes`` subparser
    outside the kanban tree, so a failure inside, say, ``chat`` during an armed
    top-level parse is refused too, and behaves exactly as stock otherwise.
    """

    def _arming_verb(self, args: list) -> Optional[str]:
        """The verb to name in the refusal when this parse arms it, else None."""
        return None

    def parse_known_args(self, args=None, namespace=None):
        args = sys.argv[1:] if args is None else list(args)
        if _REFUSING_AS.get() is not None:
            return super().parse_known_args(args, namespace)
        verb = self._arming_verb(args)
        if verb is None:
            return super().parse_known_args(args, namespace)
        token = _REFUSING_AS.set(verb)
        try:
            try:
                namespace, extras = super().parse_known_args(args, namespace)
            except argparse.ArgumentError:
                extras = True
            if extras:
                self.error("")
            return namespace, extras
        finally:
            _REFUSING_AS.reset(token)

    def error(self, message):
        verb = _REFUSING_AS.get()
        if verb is None:
            return super().error(message)
        refusal = invalid_arguments_refusal(verb)
        if not self.exit_on_error:
            raise argparse.ArgumentError(None, refusal)
        self.exit(2, refusal + "\n")


class _KanbanVerbRefusal(_ProtectedVerbRefusal):
    """Every parser of the kanban tree: arms when its argv names a protected
    verb anywhere (the ``/kanban`` surface parses here with no parent)."""

    def _arming_verb(self, args: list) -> Optional[str]:
        if not any(isinstance(word, str) and word in _PROTECTED_VERBS for word in args):
            return None
        return _resolved_verb(self, args)


class _HermesVerbRefusal(_ProtectedVerbRefusal):
    """The top-level ``hermes`` parser: arms on a protected-verb call, i.e. argv
    holds ``kanban`` and one of the four protected verbs (review r5 of #133).

    Leftovers and ``ArgumentError``s at this level (``hermes --X kanban
    request-changes ...``, ``hermes --help=X kanban request-changes ...``) used
    to reach stock argparse, which quotes them. The refusal names the verb the
    kanban parser would resolve from the words after ``kanban``, so both levels
    print the same line; without a kanban parser (or a verb after it), the
    first protected verb in argv.
    """

    def _arming_verb(self, args: list) -> Optional[str]:
        if not is_protected_verb_call(args):
            return None
        words = [word for word in args if isinstance(word, str)]
        tail = words[words.index("kanban") + 1:]
        kanban = next((action.choices["kanban"] for action in self._actions
                       if isinstance(action, argparse._SubParsersAction)
                       and "kanban" in action.choices), None)
        verbs = _verb_table(kanban) if kanban is not None else _PROTECTED_VERBS
        named = [word for word in tail if word in verbs]
        if named:
            return named[0]
        return next(word for word in words if word in _PROTECTED_VERBS)


@functools.lru_cache(maxsize=None)
def _refusing_parser_class(cls: type, mixin: type = _KanbanVerbRefusal) -> type:
    """``cls`` with ``mixin`` in front (``cls`` itself when it already has it)."""
    if issubclass(cls, mixin):
        return cls
    return type(f"{mixin.__name__.strip('_').replace('VerbRefusal', '')}{cls.__name__}",
                (mixin, cls), {})


def hermes_parser_classes(base: type = argparse.ArgumentParser) -> tuple[type, type]:
    """``(top-level class, subcommand class)`` for the full ``hermes`` parser.

    The top-level class arms the protected-verb refusal; the subcommand class
    only follows it (stock behaviour unless an armed parse is running), so the
    choke point covers every parser a protected-verb call passes through.
    ``build_parser`` upgrades the kanban tree to the kanban class itself.
    """
    return (_refusing_parser_class(base, _HermesVerbRefusal),
            _refusing_parser_class(base, _ProtectedVerbRefusal))
