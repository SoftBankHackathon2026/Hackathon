"""Secret 값 이관 — 비밀값을 메모리 bytes 로만 다루는 유일한 단계.

원본 클라우드 Secret 의 현재 값을 읽어 대상 클라우드 Secret 에 새 버전으로
쓰고, SHA-256 으로 대조한다. 값과 해시는 로그·예외·repr·파일 어디에도 남기지
않는다. 계약은 store.SecretStore, 구현은 store_aws·store_gcp, 절차는 run.
"""
