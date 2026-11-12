# The official engine remains a separate sidecar, preserving its documented entrypoint.
ARG ABRL_BROWSER_BASE=abrl-browser:local
FROM ${ABRL_BROWSER_BASE}
USER root
RUN uv pip install --python /opt/venv/bin/python kameleo.local-api-client==5.4.0
ENV ABRL_REMOTE_ADAPTER=kameleo ABRL_KAMELEO_ENDPOINT=http://127.0.0.1:5050
USER 1001
CMD ["python", "-m", "abrl.remote_browser", "browser"]
