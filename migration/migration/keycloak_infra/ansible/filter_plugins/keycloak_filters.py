from migration.keycloak_infra.discovery import cluster_report, node_record


class FilterModule:
    def filters(self):
        return {
            "keycloak_cluster_report": cluster_report,
            "keycloak_node_record": node_record,
        }
