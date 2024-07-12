# Development environment

## Prerequisites:

- [pyenv](https://github.com/pyenv/pyenv)

## Install environment

```bash
pyenv install
python -m venv venv
source venv/bin/activate
pip install .
```

## Test server

Test server that mocks the ConnectLife API. Runs on `http://localhost:8080`.

The server reads all JSON files in the current directory, and serves them as appliances. Properties can be updated,
but is not persisted. The only validation is that the `puid` and `property` exists, it assumes that all properties
are writable and that any value is legal.

```bash
cd dumps
python -m test_server
```

To use the test server, provide the URL to the test server:  
```python
from connectlife.api import ConnectLifeApi
api = ConnectLifeApi(username="user@example.com", password="password", test_server="http://localhost:8080")
```
