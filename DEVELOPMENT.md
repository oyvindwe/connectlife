# Development environment

## Prerequisites:

- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Install environment

```bash
uv sync
```

## Test server

Test server that emulates the ConnectLife API: the password login, the browser (OAuth) login, the
third-party API used with OAuth tokens, and the TRIR (Russia/CIS) backend, all at the same time.

The server reads all JSON files in a directory (by default the current one) and serves them as
appliances, so the directory must only contain appliance dumps (`python -m connectlife.dump`), not
the files from other `--query` variants. The file name becomes the device ID. Properties can be
updated but are not persisted. The only validation is that the `puid` and `property` exist; it
assumes that all properties are writable and that any value is legal.

```bash
uv run python -m connectlife.test_server [-d <dumps_dir>] [-p <port>]
```

Options:

| Option                    | Default         | Description                                                         |
|---------------------------|-----------------|---------------------------------------------------------------------|
| `-d`, `--directory`       | `.`             | Directory to read dump files from.                                  |
| `-p`, `--port`            | `8080`          | Port to serve on.                                                   |
| `-a`, `--auth_error_rate` | `0`             | Percentage of logins that fail.                                     |
| `--auth_error_type`       | `invalid_login` | Error for failed logins: `invalid_login` or `pending_registration`. |
| `--reject_tokens`         | off             | Reject access tokens when fetching appliances, forcing a re-login.  |
| `-f`, `--failure_rate`    | `0`             | Percentage of appliance fetches that fail.                          |
| `-t`, `--timeout_rate`    | `0`             | Percentage of appliance fetches that take over 10 seconds.          |
| `--missing_rate`          | `0`             | Percentage per appliance of leaving it out of an appliance fetch.   |
| `--no_status_rate`        | `0`             | Percentage per appliance of sending it without its `statusList`.    |
| `--partial_status_rate`   | `0`             | Percentage per appliance of sending about half of its `statusList`. |

To simulate an account that must accept updated Terms & Conditions, reject every login with
`Account Pending Registration`. Add `--reject_tokens` to also reject access tokens when fetching appliances, which
forces a re-login while already running:
```bash
uv run python -m connectlife.test_server -a 100 --auth_error_type pending_registration --reject_tokens
```

Energy statistics, static data, system data, self-check, hourly power and the property list return
canned responses for every appliance, not data from the dumps.

To use the test server, provide its URL to the client:
```python
from connectlife.api import ConnectLifeApi
api = ConnectLifeApi(username="user@example.com", password="password", test_server="http://localhost:8080")
```

The other clients take it the same way:
```python
from connectlife.oauth import OAuthConnectLifeApi
from connectlife.trir import TrirConnectLifeApi

async def get_access_token() -> str:
    return "my_access_token"  # The token the test server's /oauth/token returns

oauth_api = OAuthConnectLifeApi(get_access_token, "http://localhost:8080")
trir_api = TrirConnectLifeApi(username="user@example.com", password="password", test_server="http://localhost:8080")
```

The browser login page (`/login`) signs in at once and redirects back with an authorization code,
and `/oauth/token` returns fixed tokens, so an OAuth2 flow can be run against the test server too.
