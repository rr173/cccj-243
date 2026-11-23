"""Synthetic fixture generator for a narrow, fully specified contract family.

The package never reads customer data.  Its only input is a finite contract
document (JSON) that declares parent/child tables, fixed row-identity slots,
independently annotated candidate rows, and three kinds of rules:

* UNIQUE      -- a declared composite key is unique within one table
* FK          -- a child composite reference points at a parent key
* AGGREGATE   -- a signed integer sum over a child table matches the parent

Everything here is deliberately small and dependency-free so that the
resulting material can be re-run and audited without a solver stack.
"""

from .contract import Contract, ContractError, load_contract, parse_contract
from .engine import (
    Fixture,
    FixtureRequestError,
    NoFixture,
    Report,
    SearchLimitExceeded,
    brute_force_first,
    build_analysis,
    enumerate_assignments,
    evaluate_rule,
    evaluate_rules,
    generate,
    verify_fixture,
    verify_report,
)
from .material import (
    build_material,
    load_material,
    replay_material,
    write_material,
)
from .ledger import (
    FlowEntry,
    InputError,
    Ledger,
    LedgerError,
    init_ledger,
    load_fixture,
    open_ledger,
    parse_transaction,
)
from .merge import (
    Domain,
    MergeConflictError,
    MergeInputError,
    build_identity_domains,
    load_fixture_doc,
    load_whitelist,
    merge_fixtures,
    parse_whitelist,
    verify_pack,
    write_pack_atomic,
)

__all__ = [
    "Contract",
    "ContractError",
    "load_contract",
    "parse_contract",
    "Fixture",
    "FixtureRequestError",
    "NoFixture",
    "Report",
    "SearchLimitExceeded",
    "brute_force_first",
    "build_analysis",
    "enumerate_assignments",
    "evaluate_rule",
    "evaluate_rules",
    "generate",
    "verify_fixture",
    "verify_report",
    "build_material",
    "load_material",
    "replay_material",
    "write_material",
    "FlowEntry",
    "InputError",
    "Ledger",
    "LedgerError",
    "init_ledger",
    "load_fixture",
    "open_ledger",
    "parse_transaction",
    "Domain",
    "MergeConflictError",
    "MergeInputError",
    "build_identity_domains",
    "load_fixture_doc",
    "load_whitelist",
    "merge_fixtures",
    "parse_whitelist",
    "verify_pack",
    "write_pack_atomic",
]

__version__ = "0.1.0"
