"""
main.py

FastAPI wrapper exposing the Kubernetes troubleshooting agent as an HTTP
service, rather than a CLI tool invoked manually per investigation.

Endpoints:
- POST /investigate  - run a real investigation against the cluster
- GET  /metrics       - Prometheus scrape endpoint (text exposition format)
- GET  /healthz        - liveness check, does not touch the cluster or Claude

Design note: /investigate calls config.load_kube_config() once at startup,
not per-request - the Kubernetes client config rarely changes during a
service's lifetime, and re-loading it on every request would be wasted
work. If this were deployed inside the cluster it's diagnosing (rather
than run against an external cluster from a laptop), this would instead
use config.load_incluster_config() with a mounted ServiceAccount token.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from kubernetes import config
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from agent import run_investigation


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.load_kube_config()
    yield


app = FastAPI(
    title="Kubernetes Troubleshooting Agent",
    description="Agentic diagnostic service for Kubernetes pod failures.",
    lifespan=lifespan,
)


class InvestigateRequest(BaseModel):
    symptom: str
    namespace: str = "default"


class InvestigateResponse(BaseModel):
    diagnosis: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/investigate", response_model=InvestigateResponse)
def investigate(request: InvestigateRequest):
    if not request.symptom.strip():
        raise HTTPException(status_code=400, detail="symptom must not be empty")

    diagnosis = run_investigation(request.symptom, request.namespace)
    return InvestigateResponse(diagnosis=diagnosis)


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
