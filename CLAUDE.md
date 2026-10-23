# ccc-agentgen

Agent workflows for [ccc](https://github.com/rnortman/ccc), the CLI harness
that runs an agent type as a one-shot Claude Code or Codex subprocess, and the
generator that renders them: `templates/` and `variants/` are the product;
`src/agentgen/` assembles them, and `src/todotriage/` is the `todo-triage`
tool the TODO workflows run on.

A checkout of ccc beside this one, `../ccc`, is where to read harness source
when ccc's README is not enough.

Contributor conventions:

- Run `make setup-hooks` once per clone; the pre-commit hook runs
  `brenn-scrub staged` and `make check`.
- The README's **Development** section is the reference for building
  variants, the checks, the tests and the changelog.
