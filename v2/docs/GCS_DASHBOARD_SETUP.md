# HTML dashboard on Google Cloud Storage — one-time setup

Result: a private page at
`https://storage.cloud.google.com/<BUCKET>/dashboard.html`,
refreshed by the v2-dashboard workflow every 30 min during daylight.
Only Google accounts you grant can open it. No servers, no Pi required.

Time: ~15 minutes. Everything is point-and-click in the Google Cloud
console (https://console.cloud.google.com) except step 5.

## 1. Pick / create a project
Use the SAME project that owns your existing service account (the one in
GOOGLE_CREDENTIALS — open the JSON and read `project_id`). No new project
needed.

## 2. Create the bucket
Cloud Storage -> Buckets -> Create:
- Name: e.g. `argia-dashboard` (globally unique; add a suffix if taken)
- Location: `us-central1` (or any single region; irrelevant at this size)
- Storage class: Standard
- Public access prevention: **ON** (this keeps it private)
- Access control: **Uniform**

## 3. Let the pipeline write
Bucket -> Permissions -> Grant access:
- Principal: your service account email (the `client_email` field in the
  GOOGLE_CREDENTIALS JSON, looks like `...@<project>.iam.gserviceaccount.com`)
- Role: **Storage Object Admin**

## 4. Let humans read
Same Permissions screen -> Grant access:
- Principal: your Google account email (and any colleague's)
- Role: **Storage Object Viewer**

## 5. Tell GitHub the bucket name
Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:
- Name: `GCS_DASHBOARD_BUCKET`
- Value: the bucket name from step 2 (just the name, no gs:// prefix)

## 6. Verify
Actions -> "v2 Dashboard update" -> Run workflow -> dry_run **false**.
The "Publish HTML dashboard" step should print:

    [apply] uploaded to gs://<bucket>/dashboard.html — view at ...

Open `https://storage.cloud.google.com/<bucket>/dashboard.html` while logged
in to your granted Google account. Anyone NOT granted gets a 403 — that is
the access control working.

## Notes
- Before step 5 is done, the publish step prints a NOTICE and skips —
  the workflow stays green, nothing breaks.
- Cost: a few MB + light traffic = effectively zero (pennies/month).
- Later Pi/SQLite phase: the Pi runs the same publish script on its own
  schedule and uploads to the SAME bucket — the URL never changes.
