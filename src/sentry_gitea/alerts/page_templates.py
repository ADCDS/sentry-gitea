"""Markup of the settings pages (see pages.py). Django templates, autoescaped."""

# ruff: noqa: E501

STYLE = """
body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:#f6f5f8;color:#2b2233}
main{max-width:46rem;margin:0 auto;padding:1.5rem 1rem 3rem}
h1{font-size:1.4rem;margin:.2rem 0 1rem}
a{color:#5b3fb5}
.crumbs{font-size:.9rem;color:#6b6377}
fieldset{background:#fff;border:1px solid #dcd7e3;border-radius:6px;margin:0 0 1.2rem;padding:1rem 1.2rem}
legend{font-weight:600;padding:0 .4rem}
label{display:block;margin:.8rem 0 .2rem;font-weight:600}
label.check{font-weight:400;margin:.5rem 0}
input[type=text],input[type=url],input[type=password]{width:100%;box-sizing:border-box;padding:.45rem .6rem;border:1px solid #c6bfd0;border-radius:4px;font:inherit}
.help{font-size:.85rem;color:#6b6377;margin:.15rem 0 0}
.msg{padding:.6rem .9rem;border-radius:4px;margin:0 0 1rem}
.ok{background:#e6f4ea;border:1px solid #9bd0ab}
.error{background:#fdecea;border:1px solid #f0a8a0}
button{font:inherit;padding:.5rem 1rem;border-radius:4px;border:1px solid #5b3fb5;background:#5b3fb5;color:#fff;cursor:pointer}
button.secondary{background:#fff;color:#5b3fb5}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dcd7e3}
th,td{text-align:left;padding:.5rem .7rem;border-bottom:1px solid #ece8f1}
footer{margin-top:2rem;font-size:.8rem;color:#6b6377}
"""

PROJECT_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="same-origin">
<title>Gitea settings: {{ project.slug }}</title><style>{{ style|safe }}</style></head><body><main>
<p class="crumbs"><a href="{{ index_url }}">Gitea settings</a> / {{ organization.slug }} / {{ project.slug }}</p>
<h1>Gitea settings for {{ project.slug }}</h1>
{% if saved %}<p class="msg ok" role="status">Settings saved.</p>{% endif %}
{% if tested %}<p class="msg ok" role="status">Gitea accepted the saved token and the repository is reachable.</p>{% endif %}
{% if deleted %}<p class="msg ok" role="status">This project's Gitea settings and token were deleted from Sentry.</p>{% endif %}
{% if unlinked %}<p class="msg ok" role="status">Forgot the Gitea issue links of {{ unlinked }} Sentry issue(s) in this project.</p>{% endif %}
{% if error %}<p class="msg error" role="alert">{{ error }}</p>{% endif %}
<form method="post" action="{{ page_url }}" autocomplete="off">{% csrf_token %}
<fieldset><legend>Issues</legend>
<label class="check"><input type="checkbox" name="issue_enabled"{% if form.issue_enabled %} checked{% endif %}> Enabled for this project</label>
<p class="help">The connection below is checked against Gitea when you save with this box ticked. Unticked, alerts stop and the connection fields are left as saved.</p>
<label for="base_url">Gitea base URL</label>
<input type="url" id="base_url" name="base_url" value="{{ form.base_url }}" placeholder="https://gitea.example">
<p class="help">URL reachable from Sentry's containers; a hosting subpath is allowed.</p>
<label for="public_url">Gitea URL for links</label>
<input type="url" id="public_url" name="public_url" value="{{ form.public_url }}" placeholder="https://gitea.example">
<p class="help">Browser-facing URL. Leave empty when the base URL is already reachable by users.</p>
<label for="token">Personal access token</label>
<input type="password" id="token" name="token" value="" autocomplete="new-password">
<p class="help">{% if form.token_saved %}A token is saved. Leave blank to keep it; it is never shown and never sent to a different base URL.{% else %}No token is saved yet.{% endif %} Scopes: read:user, read:repository, write:issue.</p>
<label for="repo">Repository</label>
<input type="text" id="repo" name="repo" value="{{ form.repo }}" placeholder="owner/name">
<label for="default_labels">Default labels</label>
<input type="text" id="default_labels" name="default_labels" value="{{ form.default_labels }}">
<p class="help">Comma-separated label names added to every new issue.</p>
</fieldset>
<fieldset><legend>Alerts</legend>
<label class="check"><input type="checkbox" name="alerts_enabled"{% if form.alerts_enabled %} checked{% endif %}> File and update Gitea issues from this project's alert rules</label>
<p class="help">Applies to alert rules whose action sends a notification to "Gitea Alerts". Needs the issue settings above to be enabled.</p>
<label for="labels">Alert labels</label>
<input type="text" id="labels" name="labels" value="{{ form.labels }}">
<p class="help">Comma-separated labels for new alert issues.</p>
<label class="check"><input type="checkbox" name="severity_labels"{% if form.severity_labels %} checked{% endif %}> Severity and class labels</label>
<label class="check"><input type="checkbox" name="comment_on_repeat"{% if form.comment_on_repeat %} checked{% endif %}> Comment on repeat events</label>
<label class="check"><input type="checkbox" name="fix_hint"{% if form.fix_hint %} checked{% endif %}> Advisory where-to-look hint</label>
<label class="check"><input type="checkbox" name="refile_on_regression"{% if form.refile_on_regression %} checked{% endif %}> New issue when a closed issue regresses</label>
<label class="check"><input type="checkbox" name="refile_after_close"{% if form.refile_after_close %} checked{% endif %}> New issue for any event an hour after closing</label>
</fieldset>
<button type="submit" name="action" value="save">Save</button>
<button type="submit" name="action" value="test" class="secondary" formnovalidate>Test saved settings</button>
</form>
<form method="post" action="{{ page_url }}" autocomplete="off">{% csrf_token %}
<fieldset><legend>Issue links</legend>
<p class="help">Each Sentry issue remembers the Gitea issue filed for it, with the base URL and repository it was filed in. After a move to another URL or repository, alerts refuse to write through those links. Forgetting them lets the next alert for an issue find the Gitea issue that carries its marker in the repository configured now and link it again, or file a new one. Nothing changes in Gitea.</p>
<label class="check"><input type="checkbox" name="confirm" required> Forget this project's issue links</label>
</fieldset>
<button type="submit" name="action" value="unlink" class="secondary">Forget issue links</button>
</form>
<form method="post" action="{{ page_url }}" autocomplete="off">{% csrf_token %}
<fieldset><legend>Delete</legend>
<p class="help">Removes this project's Gitea settings from Sentry, the saved token included. Alerts stop filing. Links to Gitea issues that already exist are kept, and nothing changes in Gitea.</p>
<label class="check"><input type="checkbox" name="confirm" required> Delete the saved settings and token</label>
</fieldset>
<button type="submit" name="action" value="delete" class="secondary">Delete saved settings</button>
</form>
<footer><a href="{{ source_url }}">sentry-gitea</a> {{ version }}</footer>
</main></body></html>
"""

INDEX_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="same-origin">
<title>Gitea settings: {{ organization.slug }}</title><style>{{ style|safe }}</style></head><body><main>
<p class="crumbs">Gitea settings / {{ organization.slug }}</p>
<h1>Gitea settings</h1>
{% if rows %}<table><thead><tr><th>Project</th><th>Repository</th><th>Issues</th><th>Alerts</th></tr></thead><tbody>
{% for row in rows %}<tr><td><a href="{{ row.url }}">{{ row.slug }}</a></td><td>{{ row.repo }}</td><td>{{ row.issues }}</td><td>{{ row.alerts }}</td></tr>
{% endfor %}</tbody></table>{% else %}<p>You cannot change the settings of any project in this organization.</p>{% endif %}
<footer><a href="{{ source_url }}">sentry-gitea</a> {{ version }}</footer>
</main></body></html>
"""
