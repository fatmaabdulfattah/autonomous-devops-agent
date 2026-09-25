# Release checklist (v0.1.0)

## Before publishing — fill these TODOs in pyproject.toml
- [ ] `authors` (team name/email)
- [ ] `license` (e.g. MIT) + add a LICENSE file
- [ ] `project.urls.Homepage` (GitHub repo URL)

## 1. Build
```bash
pip install build twine
rm -rf dist && python -m build
twine check dist/*
```

## 2. TestPyPI (create account + API token at https://test.pypi.org)
```bash
twine upload --repository testpypi dist/*
# fresh venv, OUTSIDE the repo folder:
python -m venv /tmp/t && source /tmp/t/bin/activate
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ autonomous-devops-agent
devops --version && devops --doctor
```

## 3. Real PyPI (token from https://pypi.org/manage/account/token/)
```bash
twine upload dist/*
```
Versions can never be re-uploaded: bug after publishing → bump to 0.1.1.

## 4. Docker bundle → published package
```bash
docker compose build --build-arg INSTALL_FROM=pypi devops
PROJECT_DIR=/path/to/test-project docker compose run --rm devops --doctor
```

## 5. Tag
```bash
git tag v0.1.0 && git push --tags
```
