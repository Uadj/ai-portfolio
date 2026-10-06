"""요청 출처 IP 판별 (레이트 리밋 키).

X-Forwarded-For의 왼쪽 값은 방문자가 마음대로 넣을 수 있다 (프록시는 오른쪽에 덧붙일 뿐).
uvicorn을 forwarded_allow_ips="*"로 띄우면 request.client.host도 그 왼쪽 값이 되므로 역시 위조 가능하다.
"""
from __future__ import annotations

import os

from fastapi import Request


def client_ip(request: Request) -> str:
    h = request.headers
    # Render는 Cloudflare 뒤에 있고, Cloudflare는 이 헤더를 항상 실제 접속 IP로 덮어쓴다 (클라이언트가 주입 불가).
    # Cloudflare 뒤가 아닌 곳에 배포하면 CLIENT_IP_HEADER= 로 비워야 한다 (그대로 두면 위조 가능).
    edge = os.getenv("CLIENT_IP_HEADER", "cf-connecting-ip").strip().lower()
    v = h.get(edge, "").strip() if edge else ""
    if v:
        return v
    hosts = [x.strip() for x in h.get("x-forwarded-for", "").split(",") if x.strip()]
    # 엣지 헤더가 없을 때 XFF 오른쪽에서 몇 번째를 믿을지 (0 = 사용 안 함, 명시적으로 설정할 때만)
    hops = int(os.getenv("TRUSTED_PROXY_HOPS", "0") or 0)
    if hops and len(hosts) >= hops:
        return hosts[-hops]
    # 로컬 실행 등: 기존 동작 유지 (프록시가 없으면 XFF도 없으므로 소켓 주소)
    return (hosts[0] if hosts else "") or (request.client.host if request.client else "anon")


def header_layout(request: Request) -> str:
    """배포 환경의 프록시 헤더 구성을 IP 값 없이 요약 (TRUSTED_PROXY_HOPS 설정 확인용 로그)."""
    h = request.headers
    xff = [x for x in h.get("x-forwarded-for", "").split(",") if x.strip()]
    present = [n for n in ("cf-connecting-ip", "true-client-ip", "x-real-ip") if h.get(n)]
    return f"x-forwarded-for 항목 {len(xff)}개, 엣지 헤더: {', '.join(present) or '없음'}"
