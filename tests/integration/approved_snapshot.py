"""Publish only an approval-verified consumer snapshot after the deployed lifecycle probe."""
from urllib.parse import quote, urlencode
from finplan_contracts.validate import validate
from tests.integration.lifecycle_suite import IntegrationError


def _snapshot(response, *, dataset_id, expected_id=None):
    code,body,_=response
    if code!=200 or not isinstance(body,dict) or not isinstance(body.get('snapshot'),dict):
        raise IntegrationError('approved integration snapshot could not be read from the platform API')
    snapshot=body['snapshot']
    ident=snapshot.get('input_snapshot_id')
    if not validate({'input_snapshot_id':ident},'identifiers').valid:
        raise IntegrationError('integration snapshot response contains an invalid identifier')
    if expected_id is not None and ident!=expected_id:
        raise IntegrationError('integration snapshot read returned a different candidate identifier')
    if snapshot.get('dataset',{}).get('dataset_id')!=dataset_id:
        raise IntegrationError('integration snapshot response belongs to a different dataset')
    return snapshot


def publish_approved_integration_snapshot(transport,ssm,*,env,dataset_id,candidate_id):
    """A committed lifecycle fixture is valid for that probe, never for consumer inference."""
    if not validate({'input_snapshot_id':candidate_id},'identifiers').valid:
        raise IntegrationError('lifecycle probe returned no valid integration snapshot identifier')
    candidate=_snapshot(transport.call('GET','/v1/snapshots/'+quote(candidate_id,safe='')),dataset_id=dataset_id,expected_id=candidate_id)
    if candidate.get('status')=='approved':
        approved=candidate
    elif candidate.get('status')=='committed':
        approved=_snapshot(transport.call('GET','/v1/snapshots/latest?'+urlencode({'dataset_id':dataset_id})),dataset_id=dataset_id)
    else:
        raise IntegrationError('lifecycle snapshot has an unexpected status; no consumer pointer was published')
    if approved.get('status')!='approved':
        raise IntegrationError('consumer integration snapshot is not approved; no pointer was published')
    ssm.put_parameter(Name=f'/finplan/{env}/financialplanning/config/integration-snapshot-id',
                      Value=approved['input_snapshot_id'],Type='String',Overwrite=True)
    return approved['input_snapshot_id']
