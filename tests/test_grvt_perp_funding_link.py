"""Documented GRVT execution aliases, strictly isolated to the new claim."""
import pytest

from app.live_execution.grvt_perp_funding_link import funding_trade_times


def test_one_execution_can_link_several_fills_at_the_same_time():
    rows=[{'trade_id':'12345-1','event_time_ns':100},
          {'trade_id':'12345-2','event_time_ns':100},
          {'trade_id':'67890-1','event_time_ns':200}]
    result=funding_trade_times({'schema':'orbis.grvt.mainnet.bracket.claim.v3'},rows)
    assert result=={'12345-1':100,'12345-2':100,'12345':100,'67890-1':200,'67890':200}


@pytest.mark.parametrize('version',['v1','v2'])
def test_legacy_claims_keep_their_exact_id_contract(version):
    rows=[{'trade_id':'12345-2','event_time_ns':100}]
    assert funding_trade_times({'schema':'orbis.grvt.mainnet.bracket.claim.'+version},rows)=={'12345-2':100}


def test_different_times_in_one_execution_and_alias_collisions_fail_closed():
    claim={'schema':'orbis.grvt.mainnet.bracket.claim.v3'}
    for rows in ([{'trade_id':'123-1','event_time_ns':100},{'trade_id':'123-2','event_time_ns':200}],
                 [{'trade_id':'123-1','event_time_ns':100},{'trade_id':'123','event_time_ns':200}]):
        with pytest.raises(ValueError,match='contradictory trade times'):funding_trade_times(claim,rows)


def test_arbitrary_hyphenated_ids_do_not_create_aliases():
    rows=[{'trade_id':'owned-trade-1','event_time_ns':100},
          {'trade_id':'00123-2','event_time_ns':100}]
    assert funding_trade_times({'schema':'orbis.grvt.mainnet.bracket.claim.v3'},rows)=={
        'owned-trade-1':100,'00123-2':100}
