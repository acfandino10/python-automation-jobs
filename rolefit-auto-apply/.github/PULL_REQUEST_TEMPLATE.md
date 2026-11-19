## Summary

- 

## Test plan

- [ ] `make lint`
- [ ] `make typecheck`
- [ ] `make test`
- [ ] Manual check, if the change touches the CLI, PDF template, or a browser adapter

## Notes for reviewers

Remove API keys, résumés, and browser profiles from the diff.
New job-board adapters should include an offline test that uses a fake page session.
