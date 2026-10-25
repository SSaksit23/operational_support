# Deploy the web app: Cloud Run service "ops-agent" + Firebase Hosting in front.
# Only the files named in .gcloudignore are uploaded (never .venv or _debug).
# Hosting (firebase.json) was released once on 2026-10-24 and only needs redoing if firebase.json changes.
# Needs: gcloud signed in, Blaze plan on the project, deploy\env.yaml filled in.
$ErrorActionPreference = "Stop"
$Project = "operation-tools-330d3"
$Region = "asia-southeast1"
Set-Location $PSScriptRoot
gcloud config set project $Project
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
gcloud run deploy ops-agent --source . --region $Region --allow-unauthenticated `
  --memory 4Gi --cpu 4 --cpu-boost --no-cpu-throttling --max-instances 1 --concurrency 40 --timeout 300 --env-vars-file deploy\env.yaml
