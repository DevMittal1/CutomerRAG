import uvicorn
from app.config import settings

def main():
    print("🚀 Starting Production-Grade RAG Backend Server...")
    
    # Run the Uvicorn server programmatically with optimized production settings
    # - reload=False: Essential in production to prevent CPU overhead and process spawning lag
    # - workers=1: In a modern cloud environment (like Kubernetes, ECS, or GCP Cloud Run), horizontal
    #   scaling should be handled by the orchestrator scaling the container pods, rather than Python-level workers
    # - loop="uvloop": Enforces the high-performance uvloop ASGI loop (bundles with standard uvicorn)
    # - http="httptools": Enforces the high-speed httptools parser for rapid HTTP operations
    # - proxy_headers=True: Instructs Uvicorn to trust headers (e.g., X-Forwarded-For) injected by front gateways (Nginx, ALB, Cloudflare)
    # - forwarded_allow_ips="*": Resolves and binds client IP addresses correctly under proxy forwarding
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,              # Listen on the dynamically configured interface
        port=settings.PORT,              # Listen on the dynamically configured port
        log_level="info",                # Standard production logging depth
        reload=False,                    # Disable code reload for raw speed and security
        workers=1,                       # Pod-level scaling is handled externally by the orchestrator
        loop="uvloop",                   # High-performance event loop
        http="httptools",                # High-performance HTTP parser
        ws="websockets",                 # High-performance Websockets implementation
        proxy_headers=True,              # Trust upstream proxy correlation headers
        forwarded_allow_ips="*"          # Trust proxy IP headers from load balancer
    )

if __name__ == "__main__":
    main()
