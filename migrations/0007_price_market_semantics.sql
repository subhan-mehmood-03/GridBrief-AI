-- 0007_price_market_semantics.sql
-- Canonicalize ERCOT SPP by market so RT and DA delivery intervals never collide.

delete from timeseries legacy
where legacy.metric = 'spp'
  and exists (
      select 1
      from timeseries canonical
      where canonical.iso = legacy.iso
        and canonical.metric = 'spp_rt'
        and canonical.settlement_point = legacy.settlement_point
        and canonical.ts = legacy.ts
  );

update timeseries
set metric = 'spp_rt'
where metric = 'spp';
