from odoo import models, fields, api
import requests
import logging

_logger = logging.getLogger(__name__)


class ShopifySync(models.AbstractModel):
    _name = 'shopify.sync'
    _description = 'Shopify Synchronisatie'

    @api.model
    def _get_config(self, shop_name=None):
        domain = [('state', '=', 'connected')]
        if shop_name:
            domain.append(('shop_name', '=', shop_name))
        return self.env['shopify.config'].search(domain, limit=1)

    @api.model
    def _get_price(self, variant, config):
        if config.pricelist_id:
            try:
                return config.pricelist_id._get_product_price(variant, 1.0)
            except Exception as e:
                _logger.warning(f"Prijslijst prijs ophalen mislukt: {e}")
                return variant.lst_price
        return variant.lst_price

    @api.model
    def _get_product_images(self, product):
        images = []
        if product.image_1920:
            try:
                img_data = product.image_1920
                if isinstance(img_data, bytes):
                    img_data = img_data.decode('utf-8')
                images.append({'attachment': img_data})
            except Exception as e:
                _logger.warning(f"Hoofdafbeelding ophalen mislukt: {e}")
        try:
            for extra_img in product.product_template_image_ids:
                if extra_img.image_1920:
                    try:
                        img_data = extra_img.image_1920
                        if isinstance(img_data, bytes):
                            img_data = img_data.decode('utf-8')
                        images.append({'attachment': img_data})
                    except Exception as e:
                        _logger.warning(f"Extra afbeelding ophalen mislukt: {e}")
        except Exception as e:
            _logger.warning(f"Extra afbeeldingen ophalen mislukt: {e}")
        return images

    @api.model
    def _get_description(self, product):
        if product.shopify_description:
            return product.shopify_description
        if hasattr(product, 'website_description') and product.website_description:
            return product.website_description
        if product.description_sale:
            return product.description_sale
        return ''

    @api.model
    def _get_variant_options(self, product):
        """Haal attribuutnamen op voor Shopify options (max 3)."""
        options = []
        for line in product.attribute_line_ids:
            options.append(line.attribute_id.name)
        return options[:3]

    @api.model
    def _get_variant_option_values(self, variant):
        """Haal attribuutwaarden op voor een variant als option1/2/3."""
        values = {}
        for i, attr_value in enumerate(variant.product_template_attribute_value_ids):
            if i < 3:
                values[f'option{i + 1}'] = attr_value.name
        return values

    @api.model
    def _get_variant_option_key(self, variant):
        """Maak een unieke sleutel van de attribuutwaarden voor matching."""
        values = [
            attr_value.name
            for attr_value in variant.product_template_attribute_value_ids
        ]
        return '|'.join(values)

    @api.model
    def _match_shopify_variants(self, product, shopify_variants):
        """Koppel Shopify varianten terug aan Odoo varianten via SKU of attribuutwaarden."""
        # Bouw een lookup op basis van SKU
        shopify_by_sku = {}
        # Bouw een lookup op basis van option combinatie
        shopify_by_options = {}

        for sv in shopify_variants:
            sku = sv.get('sku', '')
            if sku:
                shopify_by_sku[sku] = sv
            # Bouw option key: option1|option2|option3
            option_parts = []
            for key in ['option1', 'option2', 'option3']:
                val = sv.get(key)
                if val and val != 'Default Title':
                    option_parts.append(val)
            if option_parts:
                option_key = '|'.join(option_parts)
                shopify_by_options[option_key] = sv

        for variant in product.product_variant_ids:
            matched = None

            # 1. Al gekoppeld via shopify_variant_id
            if variant.shopify_variant_id:
                for sv in shopify_variants:
                    if str(sv.get('id')) == str(variant.shopify_variant_id):
                        matched = sv
                        break

            # 2. Match op SKU
            if not matched and variant.default_code:
                matched = shopify_by_sku.get(variant.default_code)

            # 3. Match op attribuutwaarden
            if not matched:
                option_key = self._get_variant_option_key(variant)
                if option_key:
                    matched = shopify_by_options.get(option_key)

            if matched:
                variant.write({
                    'shopify_variant_id': str(matched.get('id')),
                    'shopify_inventory_item_id': str(matched.get('inventory_item_id')),
                })
                _logger.info(f"Variant {variant.name} gekoppeld aan Shopify variant {matched.get('id')}")
            else:
                _logger.warning(f"Geen Shopify variant gevonden voor {variant.name}")

    @api.model
    def _get_shopify_sellable_qty(self, variant, warehouse):
        """Haal verkoopbare voorraad op — fysiek minus gereserveerd."""
        location = warehouse.lot_stock_id
        quants = self.env['stock.quant'].search([
            ('product_id', '=', variant.id),
            ('location_id', '=', location.id),
        ])
        qty = sum(quants.mapped('available_quantity'))
        return max(0, int(qty))

    @api.model
    def sync_inventory_to_shopify(self, product_tmpl_id, config=None):
        """Synchroniseer voorraad van Odoo naar Shopify per locatie mapping."""
        if not config:
            config = self._get_config()
        if not config:
            _logger.error("Geen actieve Shopify configuratie gevonden")
            return False

        if not config.sync_inventory:
            return True

        product = self.env['product.template'].browse(product_tmpl_id)
        if not product.exists():
            return False

        location_mappings = self.env['shopify.location'].search([
            ('config_id', '=', config.id),
            ('sync_inventory', '=', True),
            ('warehouse_id', '!=', False),
        ])

        if not location_mappings:
            self.env.cr.execute(
                "SELECT shopify_location_id FROM shopify_config WHERE id = %s",
                (config.id,)
            )
            row = self.env.cr.fetchone()
            fallback_location = row[0] if row else False

            if not fallback_location:
                _logger.error("Geen Shopify locatie gevonden")
                return False

            for variant in product.product_variant_ids:
                if not variant.shopify_inventory_item_id:
                    continue
                try:
                    qty = max(0, int(variant.qty_available))
                    url = f"{config.shop_url}/admin/api/2025-01/inventory_levels/set.json"
                    response = requests.post(
                        url,
                        json={
                            'location_id': int(fallback_location),
                            'inventory_item_id': int(variant.shopify_inventory_item_id),
                            'available': qty,
                        },
                        headers=config._get_headers(),
                        timeout=15
                    )
                    if response.status_code == 200:
                        _logger.info(f"Voorraad {qty} gesynchroniseerd voor {variant.name}")
                    else:
                        _logger.error(f"Voorraad sync mislukt: {response.text[:200]}")
                except Exception as e:
                    _logger.error(f"Voorraad sync fout: {e}")
            return True

        success = True
        for mapping in location_mappings:
            for variant in product.product_variant_ids:
                if not variant.shopify_inventory_item_id:
                    continue
                try:
                    qty = self._get_shopify_sellable_qty(variant, mapping.warehouse_id)
                    url = f"{config.shop_url}/admin/api/2025-01/inventory_levels/set.json"
                    response = requests.post(
                        url,
                        json={
                            'location_id': int(mapping.shopify_location_id),
                            'inventory_item_id': int(variant.shopify_inventory_item_id),
                            'available': qty,
                        },
                        headers=config._get_headers(),
                        timeout=15
                    )
                    if response.status_code == 200:
                        _logger.info(f"Voorraad {qty} gesynchroniseerd voor {variant.name} naar {mapping.shopify_location_name}")
                    else:
                        _logger.error(f"Voorraad sync mislukt: {response.text[:200]}")
                        success = False
                except Exception as e:
                    _logger.error(f"Voorraad sync fout: {e}")
                    success = False

        return success

    @api.model
    def cron_sync_pending_products(self):
        """Cron job: synchroniseer alle pending producten."""
        config = self._get_config()
        if not config:
            _logger.info("Geen actieve Shopify configuratie gevonden voor cron sync")
            return

        pending = self.env['product.template'].search([
            ('shopify_published', '=', True),
            ('shopify_sync_status', '=', 'pending'),
        ])

        _logger.info(f"Cron sync: {len(pending)} producten te synchroniseren")

        for product in pending:
            try:
                self.sync_product_to_shopify(product.id, config)
            except Exception as e:
                _logger.error(f"Cron sync fout voor {product.name}: {e}")

    @api.model
    def sync_product_to_shopify(self, product_tmpl_id, config=None):
        """Synchroniseer een product van Odoo naar Shopify."""
        if not config:
            config = self._get_config()
        if not config:
            _logger.error("Geen actieve Shopify configuratie gevonden")
            return False

        product = self.env['product.template'].browse(product_tmpl_id)
        if not product.exists():
            return False

        self.env.cr.execute(
            "SELECT shopify_product_id, shopify_published FROM product_template WHERE id = %s",
            (product.id,)
        )
        row = self.env.cr.fetchone()
        shopify_product_id = row[0] if row else False
        shopify_published = row[1] if row else False

        try:
            if not shopify_published:
                if shopify_product_id:
                    url = f"{config.shop_url}/admin/api/2025-01/products/{shopify_product_id}.json"
                    response = requests.put(
                        url,
                        json={'product': {'id': shopify_product_id, 'status': 'draft'}},
                        headers=config._get_headers(),
                        timeout=15
                    )
                    if response.status_code == 200:
                        product.write({
                            'shopify_sync_status': 'synced',
                            'shopify_last_sync': fields.Datetime.now(),
                        })
                        _logger.info(f"Product {product.name} op draft gezet in Shopify")
                        return True
                    else:
                        error = response.text[:200]
                        product.write({
                            'shopify_sync_status': 'error',
                            'shopify_sync_error': error,
                        })
                        _logger.error(f"Product draft mislukt: {error}")
                        return False
                else:
                    _logger.info(f"Product {product.name} wordt niet gesynchroniseerd (niet gepubliceerd)")
                    return False

            vendor = ''
            if product.seller_ids:
                vendor = product.seller_ids[0].partner_id.name
            else:
                vendor = self.env.company.name

            tags = ''
            if product.shopify_tags:
                tags = product.shopify_tags
            elif hasattr(product, 'tag_ids') and product.tag_ids:
                try:
                    tags = ','.join(product.tag_ids.mapped('name'))
                except Exception:
                    tags = ''

            body_html = self._get_description(product)

            # Bepaal of dit product varianten heeft met attributen
            heeft_attributen = bool(product.attribute_line_ids)
            options = self._get_variant_options(product) if heeft_attributen else []

            product_data = {
                'product': {
                    'title': product.name,
                    'body_html': body_html,
                    'vendor': vendor,
                    'product_type': product.categ_id.name or '',
                    'status': 'active',
                    'tags': tags,
                    'published_scope': config.published_scope or 'global',
                    'variants': [],
                }
            }

            # Voeg options toe als het product attributen heeft
            if options:
                product_data['product']['options'] = [
                    {'name': option} for option in options
                ]

            images = self._get_product_images(product)
            if images:
                product_data['product']['images'] = images

            for variant in product.product_variant_ids:
                price = self._get_price(variant, config)
                variant_data = {
                    'price': str(price),
                    'sku': variant.default_code or '',
                    'inventory_management': 'shopify',
                    'inventory_policy': 'continue' if config.allow_backorder else 'deny',
                }

                # Attribuutwaarden als option1/2/3 alleen bij producten met attributen
                if heeft_attributen:
                    option_values = self._get_variant_option_values(variant)
                    variant_data.update(option_values)

                if hasattr(variant, 'barcode') and variant.barcode:
                    variant_data['barcode'] = variant.barcode
                if hasattr(product, 'weight') and product.weight:
                    variant_data['weight'] = product.weight
                    variant_data['weight_unit'] = 'kg'
                if variant.shopify_variant_id:
                    variant_data['id'] = variant.shopify_variant_id

                product_data['product']['variants'].append(variant_data)

            if shopify_product_id:
                url = f"{config.shop_url}/admin/api/2025-01/products/{shopify_product_id}.json"
                response = requests.put(
                    url,
                    json=product_data,
                    headers=config._get_headers(),
                    timeout=15
                )
            else:
                url = f"{config.shop_url}/admin/api/2025-01/products.json"
                response = requests.post(
                    url,
                    json=product_data,
                    headers=config._get_headers(),
                    timeout=15
                )

            if response.status_code in (200, 201):
                shopify_product = response.json().get('product', {})
                vals = {
                    'shopify_product_id': str(shopify_product.get('id')),
                    'shopify_last_sync': fields.Datetime.now(),
                    'shopify_sync_status': 'synced',
                    'shopify_sync_error': False,
                }

                # Koppel varianten terug via slimme matching
                shopify_variants = shopify_product.get('variants', [])
                self._match_shopify_variants(product, shopify_variants)

                product.write(vals)
                _logger.info(f"Product {product.name} gesynchroniseerd naar Shopify")

                if config.sync_inventory:
                    self.sync_inventory_to_shopify(product_tmpl_id, config)

                return True
            else:
                error = response.text[:200]
                product.write({
                    'shopify_sync_status': 'error',
                    'shopify_sync_error': error,
                })
                _logger.error(f"Product sync mislukt: {error}")
                return False

        except Exception as e:
            _logger.error(f"Product sync fout: {e}")
            product.write({
                'shopify_sync_status': 'error',
                'shopify_sync_error': str(e),
            })
            return False
