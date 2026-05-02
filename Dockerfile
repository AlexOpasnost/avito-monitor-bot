FROM python:3.14-slim AS runtime

# Install dependencies as root (apt access required), then drop down
# to a dedicated non-root user before running the bot. The previous
# image ran as root with full filesystem write permissions in the
# container — a remote-code-execution bug in any parser library
# would let an attacker write arbitrary files into /usr/, swap out
# system binaries, etc. With a non-root user the blast radius is
# limited to /app and /tmp.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 botuser

WORKDIR /app

# Copy and install requirements as root so site-packages is writable
# during the build and read-only at runtime — pip caches are flushed
# by --no-cache-dir.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code with ownership transferred to botuser. Anything the
# bot needs to write at runtime (the asyncio cache, etc.) ends up in
# /home/botuser, which botuser owns.
COPY --chown=botuser:botuser . .

# Drop privileges. Everything below this line runs as a UID-10001
# user with no shell and no /home/root access.
USER botuser

# Python writes .pyc files to disk during import; with the non-root
# user it would fail to write them next to the .py source. Disable
# bytecode write-back so we don't get noisy "permission denied"
# warnings on every import.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["python", "-u", "bot.py"]
