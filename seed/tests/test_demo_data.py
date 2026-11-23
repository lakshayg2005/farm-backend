"""The curated demo definitions must agree with each other and with the platform (no stack needed)."""

from catalog_service.crops import CROPS as CATALOG_CROPS
from farm_seed import demo
from farm_seed import demo_data as D
from farm_seed.photos import ASSETS

CATALOG = {c[0]: c[3] for c in CATALOG_CROPS}  # crop id -> default unit


def test_phones_are_unique():
    phones = [f.phone for f in D.FARMERS] + [c.phone for c in D.CUSTOMERS]
    phones += [p[1] for p in D.PARTNERS]
    assert len(phones) == len(set(phones))


def test_every_listing_uses_a_catalog_crop_and_has_its_photo():
    for farmer in D.FARMERS:
        for farm in farmer.farms:
            assert (ASSETS / f"{farm.photo}.jpg").exists(), farm.photo
            for item in farm.listings:
                assert item.crop in CATALOG, item.crop
                if item.photo:
                    assert (ASSETS / f"{item.photo}.jpg").exists(), item.photo


def test_unverified_farmers_have_no_farms():
    assert all(not f.farms for f in D.FARMERS if not f.verified)


def test_plots_belong_to_verified_farmers_and_offer_real_crops():
    for plot in D.PLOTS:
        assert D.FARMERS[plot.farmer].verified
        assert set(plot.crops) <= set(CATALOG)
        assert 60 <= plot.side_m <= 160  # roughly 1 to 6 acres


def test_reservations_ask_for_a_crop_the_plot_allows_and_have_photos():
    for r in D.RESERVATIONS:
        assert r.crop in D.PLOTS[r.plot].crops
        for _stage, _note, photo, _days in r.updates:
            assert (ASSETS / f"{photo}.jpg").exists(), photo
    # the two plots with a finished Kharif are the ones used by the harvest stories
    past = {r.plot for r in D.RESERVATIONS if r.season == "Kharif"}
    assert past <= set(D.PLOTS_WITH_PAST_KHARIF)


def test_orders_only_use_listings_that_exist():
    listed = {
        (i, item.crop)
        for i, f in enumerate(D.FARMERS)
        for farm in f.farms
        for item in farm.listings
    }
    for _customer, lines, fate in demo.ORDERS:
        assert fate in {"CONFIRMED", "CANCELLED", "WAITING", "FAIL_LOWER", "DISPATCH", "SHARED"}
        for farmer, crop, _qty in lines:
            assert (farmer, crop) in listed, (farmer, crop)
