import os
import re
import json
import time
import asyncio
import logging
import tempfile

from flask import Flask, render_template_string, request, session as flask_session
import pyimgbox

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BASE_DIR       = os.getcwd()
TMP_DIR        = os.path.join(BASE_DIR, "uploads_tmp")
GALLERIES_JSON = os.path.join(BASE_DIR, "galleries.json")

os.makedirs(TMP_DIR, exist_ok=True)
if not os.path.exists(GALLERIES_JSON):
    with open(GALLERIES_JSON, "w") as f:
        json.dump({}, f)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("imgbox_app")

# ─── HTML TEMPLATE (Bootstrap + DataTables) ─────────────────────────────────
INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Imgbox Uploader</title>
  <link
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css"
    rel="stylesheet">
  <link
    href="https://cdn.datatables.net/1.13.4/css/dataTables.bootstrap5.min.css"
    rel="stylesheet">
</head>
<body class="bg-light">
<div class="container py-5">
  <h1 class="mb-4">Imgbox Uploader</h1>
  <form action="/upload" method="post" enctype="multipart/form-data" class="mb-5">
    <div class="mb-3">
      <label class="form-label">Select Images</label>
      <input type="file" name="files" class="form-control" multiple
             accept="image/jpeg,image/png,image/gif" required>
    </div>
    <div class="mb-3">
      <label class="form-label">Gallery Title</label>
      <input type="text" name="title" class="form-control"
             placeholder="e.g. My Manga Chapter">
    </div>
    <div class="mb-3">
      <label class="form-label">_imgbox_session cookie</label>
      <input type="text" name="authCookie" class="form-control"
             placeholder="Paste your _imgbox_session here"
             value="{{ session_cookie }}" required>
    </div>
    <button type="submit" class="btn btn-primary">Upload</button>
  </form>

  {% if error_message %}
    <div class="alert alert-danger">{{ error_message|safe }}</div>
  {% endif %}

  {% if upload_results %}
    <div class="card mb-5">
      <div class="card-header">Upload Results</div>
      <div class="card-body">{{ upload_results|safe }}</div>
      {% if upload_time %}
        <div class="card-footer text-muted">
          Time: {{ upload_time|round(2) }} s
        </div>
      {% endif %}
    </div>
  {% endif %}

  {% if saved_links %}
    <div class="card">
      <div class="card-header">Saved Galleries</div>
      <div class="card-body">
        <table id="tbl" class="table table-striped">
          <thead>
            <tr><th>Title</th><th>Gallery</th><th>Edit</th></tr>
          </thead>
          <tbody>
          {% for t,i in saved_links.items() %}
            <tr>
              <td>{{ t }}</td>
              <td><a href="{{ i.gallery_url }}" target="_blank">View</a></td>
              <td><a href="{{ i.edit_url }}"   target="_blank">Edit</a></td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  {% endif %}
</div>

<script src="https://code.jquery.com/jquery-3.6.1.min.js"></script>
<script
  src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js">
</script>
<script
  src="https://cdn.datatables.net/1.13.4/js/jquery.dataTables.min.js">
</script>
<script
  src="https://cdn.datatables.net/1.13.4/js/dataTables.bootstrap5.min.js">
</script>
<script>
  $(document).ready(() => {
    $('#tbl').DataTable({
      pageLength: 5,
      lengthChange: false,
      info: false
    });
  });
</script>
</body>
</html>
"""

# ─── JSON STORE HELPERS ────────────────────────────────────────────────────────
def load_saved_links():
    with open(GALLERIES_JSON, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_gallery_link(title, gallery_url, edit_url):
    data = load_saved_links()
    data[title] = {"gallery_url": gallery_url, "edit_url": edit_url}
    with open(GALLERIES_JSON, "w") as f:
        json.dump(data, f, indent=2)

# ─── FLASK APP ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(24)

@app.route("/", methods=["GET"])
def index():
    return render_template_string(
        INDEX_HTML,
        session_cookie=flask_session.get("authCookie", ""),
        saved_links=load_saved_links()
    )

@app.route("/upload", methods=["POST"])
async def upload():
    session_cookie = request.form.get("authCookie", "").strip()
    if not session_cookie:
        return render_template_string(
            INDEX_HTML,
            error_message="Please provide your <code>_imgbox_session</code> cookie.",
            session_cookie="",
            saved_links=load_saved_links()
        )

    title = request.form.get("title") or "Uploaded Gallery"
    files = request.files.getlist("files")
    if not files:
        return render_template_string(
            INDEX_HTML,
            error_message="No files selected.",
            session_cookie=session_cookie,
            saved_links=load_saved_links()
        )

    # Dump to zero-padded temp files in order
    infos = []
    for idx, f in enumerate(files, start=1):
        orig = os.path.basename(f.filename)
        ext  = os.path.splitext(orig)[1] or ""
        data = f.read()
        tmp  = tempfile.NamedTemporaryFile(
            delete=False,
            prefix=f"{idx:03d}_",
            suffix=ext,
            dir=TMP_DIR
        )
        tmp.write(data)
        tmp.flush()
        tmp.close()
        infos.append({"path": tmp.name, "name": orig})

    try:
        t0 = time.monotonic()
        upload_results = await handle_uploads_sequential(infos, title, session_cookie)
        upload_time = time.monotonic() - t0
    except Exception as e:
        log.exception("Upload error")
        upload_results = f"<b>Unexpected error:</b> {e}"
        upload_time = None
    finally:
        for info in infos:
            try:
                os.remove(info["path"])
            except:
                pass

    return render_template_string(
        INDEX_HTML,
        error_message=None,
        upload_results=upload_results,
        upload_time=upload_time,
        session_cookie=session_cookie,
        saved_links=load_saved_links()
    )


async def handle_uploads_sequential(infos, title, session_cookie):
    """
    Creates or appends to an Imgbox gallery, then uploads each file
    one-by-one in order, preserving sequence.
    """
    saved   = load_saved_links().get(title, {})
    edit_url= saved.get("edit_url")

    # Instantiate gallery properly
    if edit_url:
        m = re.search(r"/upload/edit/([^/]+)/([^/]+)", edit_url)
        if m:
            slug, token = m.groups()
            gallery = pyimgbox.Gallery(slug=slug, token=token)
            log.info(f"Appending to existing gallery {slug}.")
        else:
            gallery = pyimgbox.Gallery(title=title)
            log.warning("Bad edit_url, creating new gallery.")
    else:
        gallery = pyimgbox.Gallery(title=title)
        log.info(f"Creating new gallery '{title}'")

    # Inject your session cookie into the HTTPX client
    httpx_client = gallery._client._client
    httpx_client.headers["Cookie"] = f"_imgbox_session={session_cookie}"

    results = []
    async with gallery as g:
        # Force creation & token fetch before any uploads
        await g.create()

        # Upload sequentially
        for info in infos:
            name = info["name"]
            backoff = 1
            submission = None

            # Retry up to 5 times on transient errors
            for attempt in range(5):
                try:
                    submission = await g.upload(info["path"])
                    break
                except Exception as exc:
                    if attempt == 4:
                        submission = exc
                    else:
                        log.warning(f"Retry {attempt+1} for {name}: {exc}")
                        await asyncio.sleep(backoff)
                        backoff *= 2

            # Record the outcome
            if isinstance(submission, Exception):
                results.append(f"<b>Failed:</b> {name} – {submission}")
            elif getattr(submission, "success", False):
                url = submission.web_url or submission.image_url or ""
                results.append(
                    f"<b>OK:</b> {name} → <a href='{url}' target='_blank'>{url}</a>"
                )
            else:
                err = getattr(submission, "error", "Unknown error")
                results.append(f"<b>Fail:</b> {name} – {err}")

    # Persist gallery & edit URLs
    if gallery.url:
        save_gallery_link(title, gallery.url, gallery.edit_url)
        results.insert(
            0,
            f"<b>Gallery URL:</b> <a href='{gallery.url}' target='_blank'>{gallery.url}</a>"
        )
        results.insert(
            1,
            f"<b>Edit URL:</b> <a href='{gallery.edit_url}' target='_blank'>{gallery.edit_url}</a>"
        )

    return "<br>".join(results)


if __name__ == "__main__":
    log.info("Starting Flask server at http://127.0.0.1:5000")
    app.run(debug=True, host="127.0.0.1", port=5000)
