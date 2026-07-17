# soundminer2reaper web

The browser interface for `soundminer2reaper`. It runs the existing Python
converter through Pyodide in a web worker, then returns generated REAPER chains
and a conversion report as a ZIP.

Files remain in the browser's temporary memory and are not uploaded to the site
or persisted by the application.

## Development

From this directory:

```console
npm install
npm run dev
```

The `predev` and `prebuild` scripts copy the repository's unmodified
`sm2reaper.py` into `public/`, ensuring the browser and command-line versions use
the same conversion implementation.

## Validation

```console
npm run build
python -m unittest discover -s tests -p "test_web_adapter.py" -v
```
