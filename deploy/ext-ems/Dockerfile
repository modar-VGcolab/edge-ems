# External-EMS gateway container (HIL Scenario 1). Opt-in via compose profile
# `ext-ems`; absent from the normal A-E runs.
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY ext_ems ./ext_ems
EXPOSE 5010
CMD ["python", "-m", "ext_ems.main"]
