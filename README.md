# swj-tools

Helper scripts for SWJ Search's Claude skills. Public on purpose so scheduled runs can fetch them; nothing here is secret.

- `swj_crm.py`: reads and writes the Team CRM (Supabase) for the skills. Credentials come from the environment variables `SWJ_CRM_EMAIL` and `SWJ_CRM_PASSWORD` at run time; they are never in this repo. The project URL and publishable key inside the script are public by design (the web app ships them too) and grant nothing on their own.

Source of truth is `tools/swj_crm.py` in the private `swj-crm` repo; copy it here after any change.

Fetch: `curl -sS https://raw.githubusercontent.com/Manandhisdog1982/swj-tools/main/swj_crm.py -o ~/swj_crm.py`
