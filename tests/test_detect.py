"""Tests de las reglas de detección. Correr con: python3 -m unittest"""

import unittest

from turbo import detect

DAY = 86400
NOW = 100 * DAY
RULES = dict(detect.DEFAULT_RULES)


def row(days_ago, price, global_offer=0, in_stock=1):
    return {"ts": NOW - days_ago * DAY, "price": price, "global_offer": global_offer, "in_stock": in_stock}


def product(price, real_price, **kw):
    return {"price": price, "real_price": real_price, "global_offer": 0, "global_offer_max": None,
            "prev_price": None, **kw}


class OfferStatus(unittest.TestCase):
    def status(self, rows):
        return detect.offer_status(rows, RULES, NOW)

    def test_bajo_hace_poco_es_real(self):
        o = self.status([row(20, 1000), row(2, 700)])
        self.assertEqual(o["status"], "real")
        self.assertEqual(o["ref_price"], 1000)

    def test_mismo_precio_hace_semanas_es_inflado(self):
        # El "precio oferta" es el de siempre: ya pasaron más de 7 días
        self.assertEqual(self.status([row(10, 700)])["status"], "inflado")

    def test_reajustes_chicos_cuentan_como_el_mismo_precio(self):
        self.assertEqual(self.status([row(10, 700), row(3, 707)])["status"], "inflado")

    def test_visto_hace_poco_no_se_sabe(self):
        self.assertEqual(self.status([row(3, 700)])["status"], "sin_historial")

    def test_poco_historial_antes_del_cambio_no_se_sabe(self):
        self.assertEqual(self.status([row(4, 1000), row(1, 700)])["status"], "sin_historial")

    def test_cambio_que_no_baja_lo_suficiente_es_inflado(self):
        o = self.status([row(20, 1000), row(2, 950)])
        self.assertEqual(o["status"], "inflado")
        self.assertEqual(o["ref_price"], 1000)

    def test_subio_y_le_pusieron_descuento_es_inflado(self):
        # Truco clásico: suben el precio y después "lo rebajan" a lo que costaba
        self.assertEqual(self.status([row(30, 700), row(5, 1400), row(2, 700)])["status"], "inflado")

    def test_ignora_tramos_de_promo_usuario_nuevo(self):
        rows = [row(20, 1000), row(15, 1, global_offer=1), row(12, 1000), row(2, 700)]
        o = self.status(rows)
        self.assertEqual(o["status"], "real")
        self.assertEqual(o["ref_price"], 1000)


class OfertaFuerte(unittest.TestCase):
    def rules_hit(self, p, offer):
        return [h["rule"] for h in detect.check(p, None, None, 0, RULES, False, None, offer)]

    def test_tachado_alto_sobre_precio_real_alerta(self):
        offer = {"status": "real", "since": NOW, "ref_price": 1000}
        self.assertIn("gran_descuento", self.rules_hit(product(450, 1000), offer))

    def test_tachado_inflado_no_alerta(self):
        offer = {"status": "inflado", "since": NOW - 30 * DAY, "ref_price": None}
        self.assertNotIn("gran_descuento", self.rules_hit(product(450, 1000), offer))

    def test_sin_historial_no_alerta(self):
        offer = {"status": "sin_historial", "since": NOW, "ref_price": None}
        self.assertNotIn("gran_descuento", self.rules_hit(product(450, 1000), offer))

    def test_precio_absurdo_alerta_igual(self):
        self.assertEqual(self.rules_hit(product(1, 1000), None), ["precio_absurdo"])


if __name__ == "__main__":
    unittest.main()
