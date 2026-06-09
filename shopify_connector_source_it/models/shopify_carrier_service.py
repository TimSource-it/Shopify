from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class ShopifyCarrierService(models.AbstractModel):
    _name = 'shopify.carrier.service'
    _description = 'Shopify CarrierService berekeningen'

    @api.model
    def _get_available_rates(self, destination, items, currency_code, config):
        """
        Bereken beschikbare verzendopties voor een Shopify checkout.
        destination: dict met country, postal_code, city
        items: lijst van dicts met name, quantity, grams
        currency_code: bijv. 'EUR'
        config: shopify.config record
        """
        try:
            partner = self._get_or_create_temp_partner(destination)
            if not partner:
                _logger.error("Geen partner kunnen aanmaken voor CarrierService berekening")
                return []

            total_weight_grams = sum(
                item.get('grams', 0) * item.get('quantity', 1)
                for item in items
            )
            total_weight_kg = total_weight_grams / 1000.0

            carriers = self.env['delivery.carrier'].search([
                ('active', '=', True),
            ])

            rates = []
            for carrier in carriers:
                try:
                    if not carrier._match_address(partner):
                        continue

                    price = self._calculate_carrier_price(carrier, partner, total_weight_kg)
                    if price is False:
                        continue

                    rate = {
                        'service_name': carrier.name,
                        'service_code': f'carrier_{carrier.id}',
                        'total_price': str(int(round(price * 100))),
                        'currency': currency_code or 'EUR',
                    }

                    if carrier.tracking_url:
                        rate['description'] = carrier.name

                    rates.append(rate)
                    _logger.info(f"Carrier {carrier.name} beschikbaar voor {destination.get('city')} — €{price:.2f}")

                except Exception as e:
                    _logger.warning(f"Carrier {carrier.name} berekening mislukt: {e}")
                    continue

            return rates

        except Exception as e:
            _logger.error(f"CarrierService berekening fout: {e}")
            return []

    @api.model
    def _get_or_create_temp_partner(self, destination):
        """Maak een tijdelijke partner aan voor de adrescheck."""
        try:
            country_code = destination.get('country', '')
            country = self.env['res.country'].search([
                ('code', '=', country_code)
            ], limit=1)

            partner = self.env['res.partner'].search([
                ('name', '=', 'Shopify CarrierService Temp'),
                ('active', '=', False),
            ], limit=1)

            vals = {
                'name': 'Shopify CarrierService Temp',
                'street': destination.get('address1', ''),
                'city': destination.get('city', ''),
                'zip': destination.get('postal_code', ''),
                'country_id': country.id if country else False,
                'active': False,
            }

            if destination.get('province_code') and country:
                state = self.env['res.country.state'].search([
                    ('code', '=', destination.get('province_code')),
                    ('country_id', '=', country.id),
                ], limit=1)
                if state:
                    vals['state_id'] = state.id

            if partner:
                partner.write(vals)
            else:
                partner = self.env['res.partner'].create(vals)

            return partner

        except Exception as e:
            _logger.error(f"Tijdelijke partner aanmaken mislukt: {e}")
            return False

    @api.model
    def _calculate_carrier_price(self, carrier, partner, weight_kg):
        """Bereken de prijs voor een carrier op basis van gewicht en adres."""
        try:
            if carrier.delivery_type == 'fixed':
                return carrier.fixed_price

            elif carrier.delivery_type == 'base_on_rule':
                try:
                    price = carrier._get_price_from_picking_weight(weight_kg)
                    if price is not False:
                        return price
                except Exception:
                    pass
                return False

            else:
                # Externe provider — geef vaste prijs terug als fallback
                if carrier.fixed_price > 0:
                    return carrier.fixed_price
                return False

        except Exception as e:
            _logger.warning(f"Prijs berekening mislukt voor {carrier.name}: {e}")
            return False
