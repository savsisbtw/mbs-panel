#!/bin/bash
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T=$(mktemp -d)
cd "$T"
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.autocrlf GIT_CONFIG_VALUE_0=false
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
mkdir -p stub bin sysd
cat > stub/systemctl << 'EOF'
#!/bin/sh
exit 0
EOF
chmod +x stub/systemctl
export PATH="$T/stub:$PATH"
export MBS_BACKUP_DIR="$T/backups"

PASS=0
FAIL=0
ok() { echo "PASS $1"; PASS=$((PASS+1)); }
bad() { echo "FAIL $1 :: $2"; FAIL=$((FAIL+1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1" "$3"; fi; }

sed -e "s#APP_DIR=\"/opt/mbs-panel\"#APP_DIR=\"$T/app\"#" -e "s#/usr/local/bin/mbs#$T/bin/mbs#g" -e "s#/etc/systemd/system#$T/sysd#g" "$REPO/mbs" > "$T/mbs-run"

git init -q --bare -b main origin.git
git clone -q origin.git seed 2>/dev/null
cd seed
git checkout -q -b main 2>/dev/null || true
mkdir -p systemd
cp "$REPO/mbs" mbs
echo "v1" > bot.py
echo "v1" > config.py
echo "v1" > db.py
echo "v1" > settings.py
echo "x" > requirements.txt
echo "[Service]" > systemd/mbs-bot.service
echo "ExecStart=x --workers __WORKERS__" > systemd/mbs-api.service
cp "$REPO/.gitignore" .gitignore
git add -A && git commit -q -m A && git push -q origin main
cd "$T"
git clone -q origin.git app
mkdir -p app/venv/bin
printf '#!/bin/sh\nexit 0\n' > app/venv/bin/pip
printf '#!/bin/sh\nexec python "$@"\n' > app/venv/bin/python
chmod +x app/venv/bin/pip app/venv/bin/python
echo "SECRET=1" > app/.env
python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('pragma journal_mode=wal'); c.execute('create table t(x)'); c.execute('insert into t values (42)'); c.commit(); c.close()" app/mbs.db

cd seed && echo "v2" > bot.py && echo "v2" > db.py && git commit -qam B && git push -q origin main && cd "$T"

echo "manual edit" >> app/bot.py
echo "manual edit" >> app/config.py
echo "manual edit" >> app/db.py
echo "manual edit" >> app/settings.py
OUT=$(bash "$T/mbs-run" update 2>&1); RC=$?
echo "$OUT" > out1.txt
check "update succeeds despite manual edits on the server (the reported bug)" "[ $RC -eq 0 ]" "rc=$RC $OUT"
check "bot.py is the new version" "[ \"\$(cat app/bot.py)\" = v2 ]" "$(cat app/bot.py)"
check "a stash with the manual edits exists" "[ \$(git -C app stash list | wc -l) -eq 1 ]"
check "patch with the manual edits saved" "ls app/local-changes/*.patch >/dev/null 2>&1 && grep -q 'manual edit' app/local-changes/*.patch"
check "user is told where the edits went" "grep -q 'ручные правки' out1.txt && grep -q 'патч' out1.txt"
check "working tree clean after update" "[ -z \"\$(git -C app status --porcelain --untracked-files=no)\" ]"
check "cli copied" "[ -f bin/mbs ]"
check "a pre-update backup was made" "[ \$(ls backups/mbs-before-update-*.tar.gz 2>/dev/null | wc -l) -eq 1 ]" "$(ls backups 2>&1)"
check "update output points at the backup" "grep -q 'резервная копия перед обновлением' out1.txt"
mkdir -p unpack && tar xzf backups/mbs-before-update-*.tar.gz -C unpack
APPREL="${T#/}/app"
check "backup keeps .env" "grep -q SECRET=1 unpack/$APPREL/.env"
check "backup keeps the manual edits as they were before the update" "grep -q 'manual edit' unpack/$APPREL/bot.py && grep -q 'manual edit' unpack/$APPREL/config.py"
check "backup database is a consistent sqlite copy" "[ \"\$(python -c \"import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select x from t').fetchone()[0])\" unpack/$APPREL/mbs.db)\" = 42 ]"
check "backup does not drag the venv along" "[ ! -d unpack/$APPREL/venv ]"
check "no snapshot temp file is left behind" "[ ! -e app/.mbs.db.snapshot ]"
OUT=$(bash "$T/mbs-run" backup 2>&1); RC=$?
check "mbs backup makes a manual copy" "[ $RC -eq 0 ] && ls backups/mbs-manual-*.tar.gz >/dev/null 2>&1" "$OUT"
for i in 1 2 3 4 5 6 7; do sleep 1.1; bash "$T/mbs-run" backup >/dev/null 2>&1; done
check "only the 5 newest backups are kept" "[ \$(ls backups/mbs-*.tar.gz | wc -l) -eq 5 ]" "$(ls backups | wc -l)"

OUT=$(bash "$T/mbs-run" update 2>&1); echo "$OUT" > out2.txt
check "second run says already latest" "grep -q 'уже последняя' out2.txt" "$OUT"

cd "$T/app" && git stash drop -q && git reset -q --hard HEAD; cd "$T"

git clone -q --bare origin.git mirror2.git
cd seed && git pull -q origin main 2>/dev/null; echo "v3" > bot.py && git commit -qam C && git push -q "$T/mirror2.git" main && cd "$T"
git -C mirror2.git update-server-info
python -m http.server 8799 --directory "$T" > http.log 2>&1 &
HTTP_PID=$!
sleep 1.5
OUT=$(bash "$T/mbs-run" update "http://127.0.0.1:8799/mirror2.git" 2>&1); RC=$?
echo "$OUT" > out3.txt
check "update by mirror url works" "[ $RC -eq 0 ] && [ \"\$(cat app/bot.py)\" = v3 ]" "rc=$RC $OUT"
check "url run mentions the source and how to save it" "grep -q 'источник: http://127.0.0.1:8799/mirror2.git' out3.txt && grep -q 'mbs mirror http' out3.txt" "$OUT"
check "url alone is not saved automatically" "[ ! -f app/.update_mirror ]"

bash "$T/mbs-run" mirror "http://127.0.0.1:8799/mirror2.git" > out4.txt 2>&1
check "mirror command saves the url" "grep -q 'http://127.0.0.1:8799/mirror2.git' app/.update_mirror"
check "mirror file is git-ignored" "[ -z \"\$(git -C app status --porcelain)\" ]" "$(git -C app status --porcelain)"
bash "$T/mbs-run" mirror > out5.txt 2>&1
check "mirror shows the saved url" "grep -q 'своё зеркало для обновлений: http://127.0.0.1:8799' out5.txt"

cd seed && echo "v4" > bot.py && git commit -qam D && git push -q "$T/mirror2.git" main && cd "$T"
git -C mirror2.git update-server-info
OUT=$(bash "$T/mbs-run" update 2>&1); RC=$?
echo "$OUT" > out6.txt
check "plain update prefers the saved mirror" "[ $RC -eq 0 ] && grep -q 'источник: своё зеркало' out6.txt && [ \"\$(cat app/bot.py)\" = v4 ]" "rc=$RC $OUT"

bash "$T/mbs-run" mirror off > out7.txt 2>&1
check "mirror off removes it" "[ ! -f app/.update_mirror ]"

for bad_url in "ext::sh -c 'touch $T/pwned'" "-uHEAD" "file:///etc" "ftp://x/y" "https://a b/c" "--upload-pack=touch $T/pwned2"; do
  OUT=$(bash "$T/mbs-run" update "$bad_url" 2>&1); RC=$?
  check "rejects unsafe source '$bad_url'" "[ $RC -ne 0 ] && grep -q 'не похоже на ссылку' <<< \"\$OUT\"" "rc=$RC $OUT"
done
check "no command was executed via a crafted url" "[ ! -e pwned ] && [ ! -e pwned2 ]"
OUT=$(bash "$T/mbs-run" mirror "file:///etc" 2>&1); RC=$?
check "mirror refuses unsafe urls too" "[ $RC -ne 0 ] && [ ! -f app/.update_mirror ]"

OUT=$(bash "$T/mbs-run" update "http://127.0.0.1:1/nope.git" 2>&1); RC=$?
check "unreachable mirror fails cleanly" "[ $RC -ne 0 ] && grep -q 'не удалось получить обновления по ссылке' <<< \"\$OUT\"" "rc=$RC $OUT"

cd seed && echo "v5" > bot.py && echo "broken(" > broken.py && git add -A && git commit -qm E && git push -q origin main && cd "$T"
git -C app fetch -q origin main
git -C app merge -q --ff-only origin/main~1 2>/dev/null || true
cd app && git reset -q --hard origin/main~1 2>/dev/null; cd "$T"
echo "local tweak" >> app/settings.py
BEFORE=$(git -C app rev-parse HEAD)
OUT=$(bash "$T/mbs-run" update 2>&1); RC=$?
echo "$OUT" > out8.txt
check "broken new code is rolled back" "[ $RC -ne 0 ] && grep -q 'откатываюсь' out8.txt && [ \"\$(git -C app rev-parse HEAD)\" = \"$BEFORE\" ]" "rc=$RC $OUT"
check "manual edits are restored after the rollback" "grep -q 'local tweak' app/settings.py && [ \$(git -C app stash list | wc -l) -eq 0 ]" "$(git -C app stash list)"

cd app && git checkout -q -- . && git reset -q --hard origin/main~1 && cd "$T"
cd app && echo "own" > own.txt && git add own.txt && git commit -qm "local commit" && cd "$T"
cd seed && git rm -q broken.py && echo "v6" > bot.py && git commit -qam F && git push -q origin main && cd "$T"
OUT=$(bash "$T/mbs-run" update 2>&1); RC=$?
echo "$OUT" > out9.txt
check "diverged server history is refused, not merged" "[ $RC -ne 0 ] && grep -q 'свои коммиты' out9.txt" "rc=$RC $OUT"
check "refused update leaves the local commit alone" "git -C app log --oneline | grep -q 'local commit'"

cd app && git reset -q --hard origin/main && echo "ahead" > ahead.txt && git add ahead.txt && git commit -qm ahead && cd "$T"
OUT=$(bash "$T/mbs-run" update 2>&1); RC=$?
check "server newer than the source is left alone" "[ $RC -eq 0 ] && grep -q 'версия новее' <<< \"\$OUT\"" "rc=$RC $OUT"

cd "$T/app" && git reset -q --hard origin/main && cd "$T"
cd seed && echo "v7" > bot.py && git commit -qam G && git push -q origin main && cd "$T"
BEFORE=$(git -C app rev-parse HEAD)
echo "x" > notadir
OUT=$(MBS_BACKUP_DIR="$T/notadir/x" bash "$T/mbs-run" update 2>&1); RC=$?
check "update refuses to start when no backup can be made" "[ $RC -ne 0 ] && grep -q 'не начинаю' <<< \"\$OUT\" && [ \"\$(git -C app rev-parse HEAD)\" = \"$BEFORE\" ]" "rc=$RC $OUT"
check "refused update leaves the code untouched" "[ \"\$(cat app/bot.py)\" != v7 ]"

kill $HTTP_PID 2>/dev/null
cd /
rm -rf "$T"
echo "RESULT pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
