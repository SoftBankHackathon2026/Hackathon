# 이전 완료 후 원본 CDC 후처리

RDS 관리 계정은 실제 superuser가 아니므로 CDC 계정이 소유한 publication을 삭제할 수 없다(#46). 이전 완료 시 원본 slot은 admin, publication은 설정된 cdc 계정으로 삭제한다. publication의 실제 소유자와 cdc 계정이 다르면 중단하며 소유권을 바꾸지 않는다.

자동 후처리는 전환과 최종 검증 이후에 실행한다. job의 source/sink 커넥터가 모두 없어야 하며 조회 실패도 차단한다. 해당 job의 slot/publication만 대상으로 하며 테이블·데이터·Kafka 토픽을 삭제하지 않는다. 후처리 실패는 이미 완료된 전환을 되돌리지 않고 `source_cleanup.error`에 남긴다.

완료된 checkpoint의 후처리 재시도는 별도 명령을 사용한다. 반드시 해당 이전이 완료됐고 source/target 및 job 식별자가 당시와 같은 설정인지 확인한다. 설정된 환경변수로 자격 증명을 제공한다.

```sh
python -m migration.source_cleanup --config /path/to/same-config.yaml
```

이 명령은 전환 상태 자체를 판정하지 않는다. 운영자가 완료된 job에만 실행해야 한다. 커넥터 잔존·조회 실패는 DB 변경 없이 exit 2, slot/publication 오류도 exit 2, 이미 없는 자원은 성공으로 처리한다. slot 제거 후 publication 실패라면 같은 명령을 재실행할 수 있다. 삭제된 slot은 복구할 수 없고 재이전은 새로운 CDC 준비가 필요하다.

격리 PostgreSQL 16에서 서로 다른 NOSUPERUSER admin/cdc 역할로 기존 권한 오류를 재현한다. 새 후처리 성공, 재실행, 무관한 publication과 테이블 데이터 보존을 확인한다. CI source-cleanup이 실제 DB 시험을 실행한다. 실제 RDS 귀환 후 검증은 별도다.
