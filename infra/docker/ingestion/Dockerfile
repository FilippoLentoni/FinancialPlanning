# Container-image form of the ingestion function (task 6.17), used only when
# scripts/ingestion_package_size.py reports mode "image" (pinned dependencies above the zip limit).
# Build context: a directory prepared by the build stage containing
#   requirements.txt -> `uv export --locked --no-dev --no-emit-project --extra providers` (exact pins + hashes from uv.lock)
#   vendor/          -> the pinned finplan-contracts wheel referenced by requirements.txt
#   finplan_platform/ and config/ -> the platform package and config/<env>.json
# No credentials, account identifiers or market data are copied into the image.
FROM public.ecr.aws/lambda/python:3.12-arm64
COPY requirements.txt vendor/ ${LAMBDA_TASK_ROOT}/build/
RUN pip install --no-cache-dir --require-hashes -r ${LAMBDA_TASK_ROOT}/build/requirements.txt --target ${LAMBDA_TASK_ROOT} \
    && rm -rf ${LAMBDA_TASK_ROOT}/build
COPY finplan_platform ${LAMBDA_TASK_ROOT}/finplan_platform
COPY config ${LAMBDA_TASK_ROOT}/config
ENV FINPLAN_CONFIG_DIR=/var/task/config
CMD ["finplan_platform.handlers.ingest.handler"]
