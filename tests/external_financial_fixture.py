"""Remove only new EMPTY child tables when constructing a real pre-78 fixture."""
def drop_external_financial_tables(db):
    for name in ('real_dex_external_group_links','real_dex_external_acceptance_groups',
            'real_dex_external_acceptance_links','real_dex_external_booking_members',
            'real_dex_external_booking_accounts','real_dex_external_bookings'):
        assert db.connection().exec_driver_sql('SELECT count(*) FROM '+name).scalar_one()==0
        db.connection().exec_driver_sql('DROP TABLE '+name)
