## System Tray Ingestion App

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the application:

```bash
python main.py
```

The app persists monitored folders and the global monitoring toggle in `app_state.json`.

The iRODS client session is stored separately in `irods_environment.json` and can be
edited from the settings window. Install `python-irodsclient` from `requirements.txt`
to enable background uploads.
