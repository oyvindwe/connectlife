# Python library for ConnectLife API 

Used by devices from Hisense, Gorenje, ASKO & ATAG and ETNA Connect.

The goal of this library is to support a native [Home Assistant](https://www.home-assistant.io/) integration for devices
that uses the ConnectLife API.

The code is based on [Connectlife API proxy / MQTT Home Assistant integration](https://github.com/bilan/connectlife-api-connector)
([MIT license](https://github.com/bilan/connectlife-api-connector/blob/51c6b8e4562205e1c343d0cba19354f411bd5e77/composer.json#L2-L6)).

Software is provided as is - use at your own risk. There is probably no way to harm your physical devices, but
there is no guarantee that you don't experience other problems, for instance locking your ConnectLife account. 

Licensed under [GPLv3](LICENSE).

To test out the library:
```bash
pip install connectlife
python -m connectlife.dump <username> <password>
```

This will log in to the ConnectLife API using the provided username and password, and print the list of all fields
for all appliances that is registered with the account.

The Home Assistant integration is currently in discovery phase. Please contribute your device dumps to help
the development.
