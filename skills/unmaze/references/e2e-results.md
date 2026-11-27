# End-to-end results file (`--e2e-results`)

Any tool (or person) that ran end-to-end checks on a PR can write this file. `build_artifact.py --e2e-results <file>` turns it into the board's **"Tested end to end"** section: a sidebar entry with a pass count, a one-line pill in the Overview, and one card per scenario (failures first, then skipped, then passed) with numbered steps, a GIF for each browser step and the request/response of each API or background-job step. The Markdown copy gets a short summary and a `Scenario | How | Result` table.

```json
{
  "tested_sha": "full commit sha the checks ran on",
  "tested_at": "2026-11-27T12:00:00Z",
  "environment": "Local app, tenant 1100, Chrome",
  "summary": "One plain sentence on what was tested and how it went.",
  "scenarios": [
    {
      "id": "S1",
      "title": "Filtering issues by SIEM shows only that SIEM",
      "why": "Plain words: what a user would hit if this broke.",
      "kind": "browser",
      "status": "pass",
      "note": "",
      "files": ["frontend/src/pages/issues/Filters.tsx"],
      "steps": [
        {"action": "Open Issues and pick Splunk in the SIEM filter", "expected": "Only Splunk issues are listed", "status": "pass", "gif": "media/S1-step1.gif"},
        {"action": "Call GET /api/v1/tenants/1100/issues?siem=splunk", "expected": "HTTP 200 with only Splunk issues", "status": "pass", "request": "GET /api/v1/...", "response": "HTTP 200\n{...}"}
      ]
    }
  ]
}
```

## Field rules

- **Required:** `tested_sha` (7–64 hex characters), `summary`, and a non-empty `scenarios` list. Per scenario: `id` (unique), `title`, `why`, `kind`, `status` and at least one step. Per step: `action`, `expected`, `status`.
- **Optional:** `tested_at` (ISO 8601; shown as a date and UTC time), `environment`, scenario `files`, step `gif`, `request` and `response`.
- `kind`: `browser` ("In the browser"), `api` ("Through the API") or `job` ("Background job").
- `status` (scenario and step): `pass`, `fail` or `skipped`. A `fail` or `skipped` scenario needs a `note` saying why; leave it empty for `pass`.
- `gif` is a path relative to the results file's folder and must exist. A step may have a `gif`, a `request`/`response`, both, or neither.
- `files` lists the changed files (or folders) the scenario exercises; the card links every board step whose diff touches one of them ("Proves the code in").
- When `tested_sha` isn't the commit the board was built from, the section shows an amber "older commit" warning.
- Every string is shown as plain text (HTML-escaped; `` `code` `` spans become inline code). Any problem with the file fails the build with exit code `2` and a list of what's wrong.

## GIF uploads

GIFs are never embedded in the board (the page is capped at 16 MB); they live in the Artifact's asset store.

1. Build with `--e2e-results <file> --asset-urls "$WORK_DIR/asset-urls.json"`. GIFs with no URL yet show "GIF appears after upload", and the build prints `e2e-uploads=[...]` (absolute paths) plus a `capabilities=` object that includes `"assets": {}`.
2. Publish the board with that `capabilities` object.
3. Upload the listed files with the `Artifact` tool (`action: "publish"`, `url`, `asset: true`, `file_paths`, up to 25 per call).
4. Record each returned URL by the file's sha256: `python3 scripts/e2e_results.py record-urls --asset-urls "$WORK_DIR/asset-urls.json" "<file>=<url>" ...` (merges with what's there; only `https://` and `/_blob/` URLs are used).
5. Rebuild with the same flags and republish to the same URL. An unchanged GIF keeps its URL on every later refresh.
