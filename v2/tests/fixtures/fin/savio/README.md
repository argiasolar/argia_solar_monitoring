# Savio mock data

Shapes follow https://api.savio.mx/docs (cursor pagination: `data` + `nextCursor`; `include=cfdis,items`; custom fields carry the ARGIA project / plant / milestone). Served by `server/bundle/savio_mock.py` on 127.0.0.1:8530 and replayed by `argia.fin.savio.FakeTransport` in tests.
