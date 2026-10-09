# Development environment

## Prerequisites:

- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Install environment

```bash
uv sync
```

## Test server

Test server that emulates the ConnectLife API, both normal and TRIR variants at the same time.
Runs on `http://localhost:8080`.

The server reads all JSON files in the current directory, and serves them as appliances. Properties can be updated,
but is not persisted. The only validation is that the `puid` and `property` exists, it assumes that all properties
are writable and that any value is legal.

```bash
uv run python -m connectlife.test_server
```

To simulate an account that must accept updated Terms & Conditions, reject every login with
`Account Pending Registration`. Add `--reject_tokens` to also reject access tokens when fetching appliances, which
forces a re-login while already running:
```bash
uv run python -m connectlife.test_server -a 100 --auth_error_type pending_registration --reject_tokens
```

To use the test server, provide the URL to the test server:  
```python
from connectlife.api import ConnectLifeApi
api = ConnectLifeApi(username="user@example.com", password="password", test_server="http://localhost:8080")
```
