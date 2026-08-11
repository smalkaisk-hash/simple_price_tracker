"""Parser regression tests. Run with: python -m unittest -v

No network: every fixture is a trimmed copy of markup captured from the live shop,
so a shop changing its HTML shows up as a failing test rather than a wrong alert.
"""
import unittest

import tracker

# Real Gekko listing tile, trimmed. Three different numbers live in this markup:
#   data-price="410.92"            -- the net price; x1.19 == 489.00, the shelf price
#   content="430.32" itemprop=... -- the "from" price shown on the grid (gross x 0.88)
#   489.00                         -- what the product page displays, incl. 19% VAT
# Scraping the itemprop and then stripping VAT off it under-reported by 26%.
GEKKO_TILE = '''
<article data-id="AL.Item" class="al-item" data-article-id="47674">
<div data-click-tracking="1"
     data-article-id="47674"
     data-sku="50024273;0"
     data-name="HPE 18TB 7.2k 3.5\\&quot; LFF 6G SATA Hard Drive - P38440-001 P37678-B21 MB018000GYDKR"
     data-category="SATA"
     data-brand="HPE"
     data-variant=""
     data-price="410.92"
     data-discount="0.00"
     data-category-id="34"
     data-currency-code="EUR"
     data-type="ArticleTracking">
<div data-id="AL.ItemThumbnail" class="al-item-thumbnail thumbnail">
    <a href="/en/p/HPE-18TB-7-2k-3-5-LFF-6G-SATA-Hard-Drive-P38440-001-P37678-B21-MB018000GYDKR" title="HPE 18TB">
</div>
<div class="availability green">in stock</div><div class="badge">1</div>
<div data-id="AL.ItemPrice">from <meta content="430.32" itemprop="price"/>430,32 &euro;</div>
</div>
</article>
'''

GEKKO_WATCH = next(w for w in tracker.WATCHES if w["source"] == "gekko")


class TestGekkoPrice(unittest.TestCase):
    def test_scrapes_the_net_price_not_the_from_price(self):
        (item,) = tracker.parse_gekko(GEKKO_TILE)
        self.assertEqual(item["price"], 410.92)

    def test_price_reconciles_with_the_shelf_price(self):
        """410.92 net x 1.19 VAT == 489.00, the number on the product page.

        data-price is published rounded to the cent, so the gross rebuilt from it can
        sit up to a cent off; anything wider means the wrong field is being read.
        """
        (item,) = tracker.parse_gekko(GEKKO_TILE)
        self.assertAlmostEqual(item["price"] * 1.19, 489.00, delta=0.01)

    def test_watch_declares_the_scraped_price_as_net(self):
        """The basis has to match the field, or VAT gets stripped off twice."""
        self.assertEqual(GEKKO_WATCH["price_basis"], "net")

    def test_no_vat_is_stripped_off_an_already_net_price(self):
        eur = tracker.to_eur_net(410.92, GEKKO_WATCH, {})
        self.assertAlmostEqual(eur, 410.92, places=2)

    def test_other_fields_still_parse(self):
        (item,) = tracker.parse_gekko(GEKKO_TILE)
        self.assertEqual(item["item_id"], "50024273")
        self.assertEqual(item["item_brand"], "HPE")
        self.assertTrue(item["item_name"].startswith("HPE 18TB"))
        self.assertTrue(item["in_stock"])
        self.assertEqual(item["stock_qty"], 1)
        self.assertEqual(
            item["url"],
            "https://www.gekko-computer.de/en/p/HPE-18TB-7-2k-3-5-LFF-6G-SATA-Hard-Drive"
            "-P38440-001-P37678-B21-MB018000GYDKR",
        )

    def test_tile_without_a_net_price_is_skipped(self):
        """Better to lose one drive -- and trip the "none matched" alert -- than to
        fall back to the "from" price and quietly under-report it again."""
        self.assertEqual(tracker.parse_gekko(GEKKO_TILE.replace('data-price="410.92"', "")), [])


TECHBUYER = next(w for w in tracker.WATCHES if w["name"].startswith("Techbuyer"))
RENEWTECH = next(w for w in tracker.WATCHES if w["name"].startswith("Renewtech"))
SERVERSHOP = next(w for w in tracker.WATCHES if w["source"] == "servershop24")
RATES = {"GBP": 0.85483}


class TestLatvianVat(unittest.TestCase):
    """Alerts quote the landed cost in Latvia: 21% LV VAT, never the shop's own rate.

    Intra-EU buys are reverse-charged so the German 19% never applies, and a UK import
    is zero-rated for export then charged LV import VAT at the border.
    """

    def test_lv_vat_is_added_to_a_euro_shop(self):
        self.assertAlmostEqual(tracker.to_eur_landed(100.0, SERVERSHOP, {}), 121.00, places=2)

    def test_shops_own_vat_rate_is_never_used(self):
        """Gekko is a 19% shop; 100 net must land at 121, not 119."""
        self.assertAlmostEqual(tracker.to_eur_landed(100.0, GEKKO_WATCH, {}), 121.00, places=2)

    def test_foreign_currency_converts_then_takes_lv_vat(self):
        landed = tracker.to_eur_landed(228.94, TECHBUYER, RATES)
        self.assertAlmostEqual(landed, 228.94 / 0.85483 * 1.21, places=2)

    def test_missing_fx_rate_still_returns_none(self):
        self.assertIsNone(tracker.to_eur_landed(228.94, TECHBUYER, {}))

    def test_threshold_is_measured_before_tax_and_delivery(self):
        """The bar is bare goods ex-VAT, so tax and delivery cannot change who alerts."""
        self.assertAlmostEqual(tracker.MAX_EUR_PER_TB, 25.00, places=2)


class TestShelfPrice(unittest.TestCase):
    """The shop's own displayed price, so an alert can be reconciled with the page."""

    def test_german_shops_display_gross(self):
        self.assertAlmostEqual(tracker.shop_shelf_price(410.92, GEKKO_WATCH), 489.00, delta=0.01)
        self.assertAlmostEqual(tracker.shop_shelf_price(252.09, SERVERSHOP), 299.99, delta=0.01)

    def test_techbuyer_displays_gross_at_uk_vat(self):
        self.assertAlmostEqual(tracker.shop_shelf_price(228.94, TECHBUYER), 274.73, delta=0.01)

    def test_renewtech_displays_net_so_nothing_is_added(self):
        self.assertAlmostEqual(tracker.shop_shelf_price(238.0, RENEWTECH), 238.00, delta=0.01)


class TestRanking(unittest.TestCase):
    def _rank(self, name, price, watch):
        return tracker.rank_by_price_per_tb(
            [{"item_id": "X", "item_name": name, "price": price, "in_stock": True}],
            watch, RATES)

    def test_deal_carries_both_the_landed_and_the_shelf_price(self):
        (d,) = self._rank("Toshiba 14TB SATA 6Gbps HDD", 252.09, SERVERSHOP)
        self.assertAlmostEqual(d["eur_price"], 305.03, places=2)   # 252.09 x 1.21
        self.assertAlmostEqual(d["eur_per_tb"], 305.03 / 14, places=2)
        self.assertAlmostEqual(d["shop_price"], 299.99, delta=0.01)
        self.assertEqual(d["currency"], "€")

    def test_native_price_is_still_recorded_untouched(self):
        (d,) = self._rank("Toshiba 14TB SATA 6Gbps HDD", 252.09, SERVERSHOP)
        self.assertEqual(d["price"], 252.09)


class TestShippingParsers(unittest.TestCase):
    """Trimmed copies of each shop's real shipping page, in its own currency and on the
    same VAT basis as the prices that shop displays."""

    def test_techbuyer_mainland_europe_band(self):
        page = """<h2>UK Mainland</h2><table><tbody>
        <tr><td>Order total</td><td>Delivery Price</td></tr>
        <tr><td>Up to &pound;99.99</td><td>&pound;5</td></tr></tbody></table>
        <h2 style="color: #0f89bb;">Mainland Europe</h2><table class="content-table"><tbody>
        <tr><td><strong>Order total</strong></td><td><strong>Delivery Price</strong></td></tr>
        <tr><td>Up to &pound;499.99</td><td>&pound;30</td></tr>
        <tr><td>&pound;500 +</td><td>FREE</td></tr></tbody></table>"""
        self.assertEqual(tracker.shipping_techbuyer(page), 30.0)

    def test_techbuyer_ignores_the_uk_only_band_above_it(self):
        """The UK Mainland table comes first on the page and must not be picked up."""
        self.assertNotEqual(tracker.shipping_techbuyer(
            "<h2>UK Mainland</h2><table><tr><td>Up to &pound;99.99</td><td>&pound;5</td></tr>"
            "</table><h2>Mainland Europe</h2><table><tr><td>Up to &pound;499.99</td>"
            "<td>&pound;30</td></tr></table>"), 5.0)

    def test_renewtech_eu_band(self):
        page = """<table border="1"><tr><td>Deutschland</td><td>2-3 Werktage / 15,- EUR</td></tr>
        <tr><td>L&auml;nder innerhalb der EU</td><td>2-3 Werktage / 20,- EUR</td>
        <td>1-2 Werktage / 25,- EUR</td></tr></table>"""
        self.assertEqual(tracker.shipping_renewtech(page), 20.0)

    def test_servershop24_international_flat_rate(self):
        page = ("DHL Standard Parcel Free shipping for orders over 400 &euro;"
                "<h3>DHL Standard International Parcel Shipping</h3> For all EU member states"
                "<td> flat rate up to 31,5 kg / &euro; 17,99 </td>"
                "<td>from 31,5 kg plus &euro;1,00 for each additional kg</td>")
        self.assertEqual(tracker.shipping_servershop24(page), 17.99)

    def test_gekko_latvia_first_weight_band(self):
        page = """<table><tr><th>Payment method</th><th>From 0.00 kg</th><th>From 25.01 kg</th></tr>
        <tr><td>Bank transfer</td><td>24.95 &euro;</td><td>49.95 &euro;</td></tr></table>
        <table><tr><th>Payment method</th><th>From 0.00 kg</th></tr>
        <tr><td>Bank transfer</td><td>19.95 &euro;</td></tr></table>"""
        self.assertEqual(tracker.shipping_gekko(page), 24.95)

    def test_unparseable_page_returns_none_so_the_fallback_is_used(self):
        for fn in (tracker.shipping_techbuyer, tracker.shipping_renewtech,
                   tracker.shipping_servershop24, tracker.shipping_gekko):
            self.assertIsNone(fn("<html>redesigned, nothing matches</html>"))

    def test_every_watch_declares_a_shipping_fallback(self):
        for w in tracker.WATCHES:
            self.assertIn("shipping", w, w["name"])
            self.assertIsInstance(w["shipping"]["fallback"], float, w["name"])


class TestLandedWithShipping(unittest.TestCase):
    def test_shipping_is_inside_the_vat_base(self):
        """VAT applies to the whole supply, and UK import VAT is levied on the customs
        value including freight -- so it is (goods + shipping) x 1.21, not goods x 1.21."""
        landed = tracker.to_eur_landed(252.09, SERVERSHOP, {}, shipping=17.99)
        self.assertAlmostEqual(landed, (252.09 + 17.99 / 1.19) * 1.21, places=2)

    def test_shipping_vat_comes_off_at_the_shops_display_rate(self):
        """A shop that displays gross quotes gross shipping too."""
        self.assertAlmostEqual(tracker.shipping_to_eur_net(17.99, SERVERSHOP, {}),
                               17.99 / 1.19, places=2)
        self.assertAlmostEqual(tracker.shipping_to_eur_net(20.0, RENEWTECH, {}),
                               20.0, places=2)  # Renewtech displays net

    def test_gbp_shipping_is_converted(self):
        got = tracker.shipping_to_eur_net(30.0, TECHBUYER, RATES)
        self.assertAlmostEqual(got, 30.0 / 1.20 / 0.85483, places=2)

    def test_zero_shipping_matches_the_goods_only_figure(self):
        self.assertAlmostEqual(tracker.to_eur_landed(100.0, SERVERSHOP, {}, shipping=0.0),
                               121.00, places=2)

    def test_free_over_threshold_waives_shipping(self):
        """Techbuyer ships Mainland Europe free over GBP 500 of order value."""
        self.assertEqual(tracker.shipping_charged(600.0, TECHBUYER, 30.0), 0.0)
        self.assertEqual(tracker.shipping_charged(400.0, TECHBUYER, 30.0), 30.0)

    def test_shops_without_a_threshold_always_charge(self):
        self.assertEqual(tracker.shipping_charged(9999.0, SERVERSHOP, 17.99), 17.99)

    def test_deal_reports_both_the_bare_and_the_delivered_per_tb(self):
        """Alerts show €/TB before 21% VAT and delivery, then €/TB with both."""
        (d,) = tracker.rank_by_price_per_tb(
            [{"item_id": "X", "item_name": "Toshiba 14TB SATA 6Gbps HDD",
              "price": 252.09, "in_stock": True}], SERVERSHOP, RATES, shipping=17.99)
        self.assertAlmostEqual(d["eur_net_price"], 252.09, places=2)
        self.assertAlmostEqual(d["eur_net_per_tb"], 252.09 / 14, places=2)
        self.assertLess(d["eur_net_per_tb"], d["eur_per_tb"])

    def test_bare_per_tb_is_converted_for_a_gbp_shop(self):
        (d,) = tracker.rank_by_price_per_tb(
            [{"item_id": "X", "item_name": "WD 12TB SATA 6Gbps HDD",
              "price": 228.94, "in_stock": True}], TECHBUYER, RATES, shipping=30.0)
        self.assertAlmostEqual(d["eur_net_price"], 228.94 / 0.85483, places=2)
        self.assertAlmostEqual(d["eur_net_per_tb"], 228.94 / 0.85483 / 12, places=2)

    def test_formatted_deal_carries_both_per_tb_figures(self):
        (d,) = tracker.rank_by_price_per_tb(
            [{"item_id": "X", "item_name": "Toshiba 14TB SATA 6Gbps HDD",
              "price": 252.09, "in_stock": True}], SERVERSHOP, RATES, shipping=17.99)
        text = tracker.format_deal(d, "https://example.com/x")
        self.assertIn(f"€{d['eur_net_per_tb']:.2f}/TB", text)
        self.assertIn(f"€{d['eur_per_tb']:.2f}/TB", text)
        self.assertIn("299.99", text)   # the shop's own page price
        self.assertIn("17.99", text)    # its delivery charge

    def test_deal_reports_the_delivered_total_and_the_shops_own_numbers(self):
        (d,) = tracker.rank_by_price_per_tb(
            [{"item_id": "X", "item_name": "Toshiba 14TB SATA 6Gbps HDD",
              "price": 252.09, "in_stock": True}], SERVERSHOP, RATES, shipping=17.99)
        self.assertAlmostEqual(d["eur_price"], (252.09 + 17.99 / 1.19) * 1.21, places=2)
        self.assertAlmostEqual(d["eur_delivery"], 17.99 / 1.19, places=2)
        self.assertAlmostEqual(d["eur_per_tb"], d["eur_price"] / 14, places=2)
        self.assertEqual(d["shop_shipping"], 17.99)
        self.assertAlmostEqual(d["shop_price"], 299.99, delta=0.01)


class TestThresholdBasis(unittest.TestCase):
    """Tax and delivery are shown, never used to decide who alerts or who ranks first.

    Delivery is per order and flat, so it penalises small drives hardest; letting it
    into the ranking would reorder the list purely by capacity.
    """

    def _rank(self, shipping):
        items = [{"item_id": "cheap", "item_name": "Seagate 20TB SATA 6Gbps HDD",
                  "price": 480.0, "in_stock": True},
                 {"item_id": "dear", "item_name": "Toshiba 4TB SATA 6Gbps HDD",
                  "price": 110.0, "in_stock": True}]  # 27.50/TB, over the bar
        return tracker.rank_by_price_per_tb(items, SERVERSHOP, RATES, shipping=shipping)

    def test_ranking_order_is_unaffected_by_delivery(self):
        self.assertEqual([d["sku"] for d in self._rank(0.0)],
                         [d["sku"] for d in self._rank(200.0)])

    def test_ranking_is_sorted_on_the_pre_tax_figure(self):
        ranked = self._rank(17.99)
        self.assertEqual([d["eur_net_per_tb"] for d in ranked],
                         sorted(d["eur_net_per_tb"] for d in ranked))

    def test_a_drive_qualifies_on_goods_alone(self):
        """24.25/TB of goods clears a 25.00 bar even though delivery lifts the
        delivered figure well past it."""
        ranked = self._rank(17.99)
        cheap = next(d for d in ranked if d["sku"] == "cheap")
        self.assertLess(cheap["eur_net_per_tb"], tracker.MAX_EUR_PER_TB)
        self.assertGreater(cheap["eur_per_tb"], tracker.MAX_EUR_PER_TB)
        self.assertTrue(tracker.qualifies(cheap, tracker.MAX_EUR_PER_TB))

    def test_a_dear_drive_still_fails_the_bar(self):
        dear = next(d for d in self._rank(17.99) if d["sku"] == "dear")
        self.assertFalse(tracker.qualifies(dear, tracker.MAX_EUR_PER_TB))


class TestCostBreakdown(unittest.TestCase):
    def _deal(self, watch, price, shipping):
        (d,) = tracker.rank_by_price_per_tb(
            [{"item_id": "X", "item_name": "Toshiba 14TB SATA 6Gbps HDD",
              "price": price, "in_stock": True}], watch, RATES, shipping=shipping)
        return d

    def test_the_parts_add_up_to_the_total_exactly(self):
        """Shown to the cent in every alert, so rounding the parts independently and
        letting them miss the total by a cent is a visible contradiction."""
        for watch, price, ship in ((SERVERSHOP, 252.09, 17.99), (TECHBUYER, 243.48, 30.0),
                                   (GEKKO_WATCH, 410.92, 24.95), (RENEWTECH, 238.0, 20.0)):
            d = self._deal(watch, price, ship)
            self.assertEqual(round(d["eur_net_price"] + d["eur_delivery"] + d["eur_vat"], 2),
                             d["eur_price"], watch["name"])

    def test_vat_is_charged_on_goods_and_delivery_together(self):
        d = self._deal(SERVERSHOP, 252.09, 17.99)
        self.assertAlmostEqual(d["eur_vat"],
                               (d["eur_net_price"] + d["eur_delivery"]) * 0.21, places=2)

    def test_delivery_is_shown_net_of_the_shops_own_vat(self):
        d = self._deal(SERVERSHOP, 252.09, 17.99)
        self.assertAlmostEqual(d["eur_delivery"], 17.99 / 1.19, places=2)

    def test_parts_add_up_for_a_gbp_shop_too(self):
        d = self._deal(TECHBUYER, 228.94, 30.0)
        self.assertAlmostEqual(d["eur_net_price"] + d["eur_delivery"] + d["eur_vat"],
                               d["eur_price"], places=2)
        self.assertAlmostEqual(d["eur_delivery"], 30.0 / 1.20 / 0.85483, places=2)

    def test_waived_delivery_shows_as_zero(self):
        d = self._deal(TECHBUYER, 600.0, 30.0)  # over the GBP 500 free-delivery bar
        self.assertEqual(d["eur_delivery"], 0.0)
        self.assertEqual(d["shop_shipping"], 0.0)

    def test_formatted_deal_itemises_tax_and_delivery(self):
        text = tracker.format_deal(self._deal(SERVERSHOP, 252.09, 17.99))
        for part in ("eur_net_price", "eur_delivery", "eur_vat", "eur_price"):
            self.assertIn(f"{self._deal(SERVERSHOP, 252.09, 17.99)[part]:.2f}", text, part)


class TestHistoryBasis(unittest.TestCase):
    def test_weekly_trend_ignores_entries_from_the_old_basis(self):
        """Pre-VAT history would read as a 21% overnight jump, so it is skipped."""
        history = [{"date": "2026-01-01", "best_eur_per_tb": 22.29, "basis": "eur_ex_vat"},
                   {"date": "2026-01-02", "best_eur_per_tb": 27.00,
                    "basis": tracker.HISTORY_BASIS}]
        self.assertIsNone(tracker.trend_baseline(history, "2026-01-01"))
        self.assertEqual(tracker.trend_baseline(history, "2026-01-02"), 27.00)


if __name__ == "__main__":
    unittest.main()
