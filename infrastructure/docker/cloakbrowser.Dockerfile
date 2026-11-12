# syntax=docker/dockerfile:1
ARG ABRL_BROWSER_BASE=abrl-browser:local
FROM ${ABRL_BROWSER_BASE}
USER root
RUN uv pip install --python /opt/venv/bin/python cloakbrowser==0.7.2
ENV CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser ABRL_REMOTE_ADAPTER=cloakbrowser
# A valid license is required by the vendor even to prefetch the supported binary.
# BuildKit keeps the secret out of build arguments, layers and image metadata.
RUN --mount=type=secret,id=cloak_license,required=true \
    CLOAKBROWSER_LICENSE_KEY="$(cat /run/secrets/cloak_license)" python -m cloakbrowser install \
    && rm -f /opt/cloakbrowser/license.key \
    && chown -R 10001:10001 /opt/cloakbrowser
USER 10001
CMD ["python", "-m", "abrl.remote_browser", "browser"]
