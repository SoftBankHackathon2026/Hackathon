"""감사 기록 이관 최소판 — 원본 바이트 그대로 옮기고, 옮긴 뒤 검증한다.

*   object_store: S3·GCS 를 같은 계약(ObjectStore)으로 읽고 쓴다.
*   copier: 원본 바이트를 변환 없이 복사하고 객체별 SHA-256 을 남긴다.
*   archive_files: bundle.json(AWS→GCP)·manifest.json(GCP→AWS) 형식.
*   cloudtrail_verify: 옮긴 CloudTrail 다이제스트 체인의 오프라인 검증기.
*   archive: 두 방향 이관과 이관 결과 검증의 진입점.

로그 내용은 해석하지 않는다. 다이제스트(서명 대상)만 읽는다.
"""
