# Development environment

## Prerequisites:

- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Install environment

```bash
uv sync
```

## Test server

Test server that mocks the ConnectLife API. Runs on `http://localhost:8080`.

The server reads all JSON files in the current directory, and serves them as appliances. Properties can be updated,
but is not persisted. The only validation is that the `puid` and `property` exists, it assumes that all properties
are writable and that any value is legal.

```bash
uv run python -m connectlife.test_server
```

To use the test server, provide the URL to the test server:  
```python
from connectlife.api import ConnectLifeApi
api = ConnectLifeApi(username="user@example.com", password="password", test_server="http://localhost:8080")
```
