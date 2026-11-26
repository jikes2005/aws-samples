ARG VENDOR_IMAGE
FROM ${VENDOR_IMAGE}

# The user the original image runs as (docker image inspect <image> --format '{{.Config.User}}')
ARG VENDOR_USER=10001

USER root
COPY entrypoint.sh /wrapper/entrypoint.sh
# Strip a UTF-8 BOM / Windows line endings in case the file was edited on Windows
RUN sed -i '1s/^\xEF\xBB\xBF//; s/\r$//' /wrapper/entrypoint.sh && chmod 755 /wrapper/entrypoint.sh
USER ${VENDOR_USER}

ENTRYPOINT ["/bin/sh", "/wrapper/entrypoint.sh"]
CMD ["python", "/app/app.py"]
