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

The iRODS connection settings are stored in `irods_environment.json` and can be
edited from the settings window. Passwords are kept only for the current app
session. Install `python-irodsclient` from `requirements.txt` to enable
background uploads.
