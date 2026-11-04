"""Evidence graph with distinct identities, roles, resource policies and trust."""
from .models import Inventory, digest


def normalize(inventory: Inventory) -> dict:
    nodes, edges = {}, []

    def node(kind, provider, scope, reference, **data):
        key = digest({'kind': kind, 'provider': provider, 'scope': scope, 'reference': reference})
        nodes[key] = {'id': key, 'kind': kind, 'provider': provider, 'scope': scope, 'reference': reference, **data}
        return key

    identities = []
    for label, side in (('source', inventory.source), ('target', inventory.target)):
        ep = side['endpoint']
        provider, scope = ep['provider'], ep['scope']
        selected = [i for i in side['identities'] if ep['identity'] in {i.get('id'), i.get('stable_id'), i.get('name'), i.get('arn')}]
        identity = node('execution_identity', provider, scope, selected or ep['identity'], side=label)
        for collected in side['identities']:
            collected_id = node('collected_identity', provider, scope, collected.get('id') or collected.get('stable_id') or digest(collected), raw=collected)
            if collected in selected:
                edges.append({'kind':'execution_identity_selection', 'from':collected_id, 'to':identity})
        identities.append(identity)
        resource = node('resource', provider, scope, ep['resource'], side=label)
        for policy in side['policies']:
            kind = policy.get('kind')
            if kind == 'gcp.role' and isinstance(policy.get('name'),str):
                node('permission_role', provider, scope, policy['name'], raw=policy)
            elif kind == 'gcp.allow_policy' and isinstance(policy.get('policy'),dict) and isinstance(policy.get('resource'),str):
                owner = node('resource', provider, scope, policy['resource'], side=label)
                policy_id = node('resource_policy', provider, scope, digest(policy), raw=policy)
                edges.append({'kind': 'resource_owns_policy', 'from': owner, 'to': policy_id})
                for binding in policy.get('policy', {}).get('bindings', []):
                    if not isinstance(binding,dict) or not isinstance(binding.get('members'),list):
                        continue
                    role = node('permission_role', provider, scope, binding.get('role'))
                    for member in binding.get('members', []):
                        subject = node('policy_principal', provider, scope, member)
                        edges.append({'kind': 'resource_role_binding', 'resource': owner, 'from': subject, 'to': role,
                                      'condition': binding.get('condition'), 'evidence': policy_id})
            else:
                policy_id = node('policy_evidence', provider, scope, digest(policy), raw=policy)
                statements = policy.get('Statement', [])
                statements = statements if isinstance(statements, list) else [statements]
                attached = provider == 'aws' and statements and all(isinstance(s, dict) and 'Principal' not in s and 'NotPrincipal' not in s for s in statements)
                edges.append({'kind': 'permission_attachment' if attached else 'uninterpreted_policy_evidence',
                              'from': identity if attached else resource, 'to': policy_id})
        for trust in side['trust']:
            edges.append({'kind': 'identity_use_or_delegation_evidence', 'to': identity, 'raw': trust})
        for relation in side.get('relationships', []):
            edges.append({'kind': 'collected_relationship_evidence', 'provider': provider, 'scope': scope, 'raw': relation})
    edges.append({'kind': 'migration_identity_mapping', 'from': identities[0], 'to': identities[1]})
    return {'nodes': sorted(nodes.values(), key=lambda n: n['id']), 'edges': sorted(edges, key=digest),
            'unresolved': [f'{label}:{key}:{value}' for label, side in (('source', inventory.source), ('target', inventory.target))
                           for key, value in sorted(side['coverage'].items()) if value not in {'complete', 'not_applicable'}]}
