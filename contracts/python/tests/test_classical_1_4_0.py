"""Traditional analyses use strict issued references and bounded, storage-free requests."""
from __future__ import annotations

import copy

import pytest
from finplan_contracts.validate import validate

ANALYSIS_ID = 'ca_' + '1' * 32


MIN_VARIANCE = 'tools/generate-portfolio-recommendation-min-variance-request'
CVAR = 'tools/generate-portfolio-recommendation-cvar-request'
PPO = 'tools/generate-portfolio-recommendation-ppo-request'
UNIVERSE = ['AAPL', 'AMZN', 'GOOGL', 'META', 'MSFT', 'NVDA', 'TSLA', 'VOO']


@pytest.mark.parametrize(('schema', 'document'), [
    (MIN_VARIANCE, {'instruments': UNIVERSE}),
    (MIN_VARIANCE, {'instruments': UNIVERSE, 'settings': {'lookback_days': 120, 'max_weight': .6}}),
    (CVAR, {'instruments': UNIVERSE, 'settings': {'cvar_alpha': .9}}),
    (PPO, {'instruments': UNIVERSE}),
])
def test_valid_optimization_settings(schema, document):
    assert validate(document, schema).valid


@pytest.mark.parametrize('schema', [MIN_VARIANCE, CVAR])
@pytest.mark.parametrize('document', [
    {'settings':{'lookback_days':2000}},
    {'settings':{'cash_weight':.9}},
    {'settings':{'risk_aversion':2}},
    {'algorithm':'min_variance'},
    {'algorithm':'ppo'},
    {'settings':{'arbitrary_factor':10}},
    {'as_of':'2026-01-09'},
    {'input_snapshot_id':'snap_01JA2B3C4D5E6F7G8H9JKMNPQR'},
    {'output_uri':'s3:'+'//not-accepted/path'},
])
def test_invalid_optimization_requests_are_rejected(schema, document):
    assert not validate({'instruments': UNIVERSE, **document}, schema).valid


@pytest.mark.parametrize('schema', [MIN_VARIANCE, CVAR, PPO])
@pytest.mark.parametrize('instruments', [None, [], ['AAPL', 'AAPL'], [''], ['X' * 65], [f'S{i}' for i in range(101)]])
def test_instruments_are_required_unique_and_bounded(schema, instruments):
    document = {'synthetic': True} if instruments is None else {'instruments': instruments}
    assert not validate(document, schema).valid


def test_cvar_alpha_is_only_a_cvar_setting():
    assert validate({'instruments': UNIVERSE, 'settings': {'cvar_alpha': .9}}, CVAR).valid
    assert not validate({'instruments': UNIVERSE, 'settings': {'cvar_alpha': .9}}, MIN_VARIANCE).valid


def test_analysis_identifier_is_distinct_and_wrong_kind_is_invalid_identifier():
    assert validate({'analysis_id':ANALYSIS_ID}, 'identifiers').valid
    result=validate({'analysis_id':'run_01JA2B3C4D5E6F7G8H9JKMNPQR'}, 'tools/get-classical-analysis-request')
    assert result.code=='INVALID_IDENTIFIER'
    assert result.primary().field=='analysis_id'


def test_paid_research_needs_confirmation_and_idempotency():
    name='tools/run-portfolio-research-request'
    assert validate({'review_id':ANALYSIS_ID},name).valid
    for request in [
        {'review_id':ANALYSIS_ID,'dry_run':False},
        {'review_id':ANALYSIS_ID,'dry_run':False,'confirmed_by_user':False,'idempotency_key':'key'},
        {'review_id':ANALYSIS_ID,'dry_run':False,'confirmed_by_user':True},
    ]:
        assert not validate(request,name).valid
    assert validate({'review_id':ANALYSIS_ID,'dry_run':False,'confirmed_by_user':True,'idempotency_key':'key'},name).valid


def test_no_ppo_identifiers_are_required_for_classical_evidence(store):
    import json
    doc=json.loads((store.fixtures_dir('classical-analysis')/'valid/evidence.json').read_text())
    assert validate(doc,'tools/get-classical-analysis-response').valid
    invalid=copy.deepcopy(doc)
    del invalid['analysis_ref']['checksum']
    assert not validate(invalid,'tools/get-classical-analysis-response').valid


def test_new_optional_issuer_is_additive_and_existing_issuer_changes_break():
    from finplan_contracts.compat import ADDITIVE, BREAKING, diff_schema
    old = {'type':'object','$defs':{'run_id':{'type':'string'}},'properties':{'run_id':{'$ref':'#/$defs/run_id'}},'x-finplan-minted-by':{'run_id':'financemodel'}}
    new = copy.deepcopy(old)
    new['$defs']['analysis_id']={'type':'string'}
    new['properties']['analysis_id']={'$ref':'#/$defs/analysis_id'}
    new['x-finplan-minted-by']['analysis_id']='financemodel'
    assert all(change.kind==ADDITIVE for change in diff_schema('identifiers',old,new))
    for modified in ['financialplanning',None]:
        bad=copy.deepcopy(new)
        if modified is None: del bad['x-finplan-minted-by']['run_id']
        else: bad['x-finplan-minted-by']['run_id']=modified
        assert any(change.kind==BREAKING for change in diff_schema('identifiers',old,bad))
