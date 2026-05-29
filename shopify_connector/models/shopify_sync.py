from odoo import models, fields, api
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
                images.append(img_data)
            except Exception as e:
                _logger.warning(f"Hoofdafbeelding ophalen mislukt: {e}")
        try:
            if hasattr(product, 'product_template_image_ids'):
                for extra_img in product.product_template_image_ids:
                    if extra_img.image_1920:
                        try:
                            img_data = extra_img.image_1920
                            if isinstance(img_data, bytes):
                                img_data = img_data.decode('utf-8')
                            images.append(img_data)
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
    def _get_variant_option_key(self, variant):
        values = [
            attr_value.name
            for attr_value in variant.product_template_attribute_value_ids
        ]
        return '|'.join(values)

    @api.model
    def _get_shopify_sellable_qty(self, variant, warehouse):
        location = warehouse.lot_stock_id
        quants = self.env['stock.quant'].search([
            ('product_id', '=', variant.id),
            ('location_id', '=', location.id),
        ])
        qty = sum(quants.mapped('available_quantity'))
        return max(0, int(qty))

    @api.model
    def sync_inventory_to_shopify(self, product_tmpl_id, config=None):
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
                    self._set_inventory_graphql(config, variant.shopify_inventory_item_id, fallback_location, qty)
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
                    result = self._set_inventory_graphql(
                        config, variant.shopify_inventory_item_id,
                        mapping.shopify_location_id, qty
                    )
                    if result:
                        _logger.info(f"Voorraad {qty} gesynchroniseerd voor {variant.name} naar {mapping.shopify_location_name}")
                    else:
                        success = False
                except Exception as e:
                    _logger.error(f"Voorraad sync fout: {e}")
                    success = False

        return success

    @api.model
    def _set_inventory_graphql(self, config, inventory_item_id, location_id, qty):
        mutation = """
        mutation inventorySetOnHandQuantities($input: InventorySetOnHandQuantitiesInput!) {
          inventorySetOnHandQuantities(input: $input) {
            userErrors {
              field
              message
            }
            inventoryAdjustmentGroup {
              reason
              changes {
                name
                delta
              }
            }
          }
        }
        """
        variables = {
            'input': {
                'reason': 'correction',
                'setQuantities': [{
                    'inventoryItemId': f"gid://shopify/InventoryItem/{inventory_item_id}",
                    'locationId': f"gid://shopify/Location/{location_id}",
                    'quantity': qty,
                }]
            }
        }
        data = config._graphql(mutation, variables)
        if data:
            errors = data.get('inventorySetOnHandQuantities', {}).get('userErrors', [])
            if errors:
                _logger.error(f"Voorraad GraphQL fout: {errors}")
                return False
            return True
        return False

    @api.model
    def cron_sync_pending_products(self):
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
                    return self._set_product_status(config, shopify_product_id, 'DRAFT', product)
                else:
                    _logger.info(f"Product {product.name} wordt niet gesynchroniseerd (niet gepubliceerd)")
                    return False

            vendor = product.seller_ids[0].partner_id.name if product.seller_ids else self.env.company.name

            tags = []
            if product.shopify_tags:
                tags = [t.strip() for t in product.shopify_tags.split(',')]
            elif hasattr(product, 'tag_ids') and product.tag_ids:
                try:
                    tags = product.tag_ids.mapped('name')
                except Exception:
                    tags = []

            body_html = self._get_description(product)
            heeft_attributen = bool(product.attribute_line_ids)

            if heeft_attributen:
                return self._sync_product_with_variants(
                    config, product, shopify_product_id,
                    vendor, tags, body_html
                )
            else:
                return self._sync_simple_product(
                    config, product, shopify_product_id,
                    vendor, tags, body_html
                )

        except Exception as e:
            _logger.error(f"Product sync fout: {e}")
            product.write({'shopify_sync_status': 'error', 'shopify_sync_error': str(e)})
            return False

    @api.model
    def _sync_simple_product(self, config, product, shopify_product_id, vendor, tags, body_html):
        """Sync enkelvoudig product zonder varianten."""
        variant = product.product_variant_ids[0] if product.product_variant_ids else False
        if not variant:
            return False

        price = self._get_price(variant, config)

        if shopify_product_id:
            # Update bestaand enkelvoudig product via productUpdate
            update_mutation = """
            mutation productUpdate($product: ProductUpdateInput!) {
              productUpdate(product: $product) {
                product {
                  id
                  legacyResourceId
                  variants(first: 1) {
                    nodes {
                      id
                      legacyResourceId
                      inventoryItem {
                        id
                        legacyResourceId
                      }
                    }
                  }
                }
                userErrors {
                  field
                  message
                }
              }
            }
            """
            update_input = {
                'id': f"gid://shopify/Product/{shopify_product_id}",
                'title': product.name,
                'descriptionHtml': body_html,
                'vendor': vendor,
                'productType': product.categ_id.name or '',
                'status': 'ACTIVE',
                'tags': tags,
            }
            data = config._graphql(update_mutation, {'product': update_input})
            if not data:
                product.write({'shopify_sync_status': 'error', 'shopify_sync_error': 'productUpdate mislukt'})
                return False

            errors = data.get('productUpdate', {}).get('userErrors', [])
            if errors:
                _logger.error(f"productUpdate fout voor {product.name}: {errors}")
                product.write({'shopify_sync_status': 'error', 'shopify_sync_error': str(errors)})
                return False

            shopify_product = data.get('productUpdate', {}).get('product', {})
            shopify_variants = shopify_product.get('variants', {}).get('nodes', [])
            if shopify_variants:
                sv = shopify_variants[0]
                variant.write({
                    'shopify_variant_id': str(sv.get('legacyResourceId')),
                    'shopify_inventory_item_id': str(sv.get('inventoryItem', {}).get('legacyResourceId', '')),
                })

            # Update prijs via productVariantsBulkUpdate
            if variant.shopify_variant_id:
                self._update_variant_price(config, shopify_product_id, variant, price)

        else:
            # Nieuw enkelvoudig product via productSet
            create_mutation = """
            mutation productSet($synchronous: Boolean!, $input: ProductSetInput!) {
              productSet(synchronous: $synchronous, input: $input) {
                product {
                  id
                  legacyResourceId
                  variants(first: 1) {
                    nodes {
                      id
                      legacyResourceId
                      inventoryItem {
                        id
                        legacyResourceId
                      }
                    }
                  }
                }
                userErrors {
                  field
                  message
                  code
                }
              }
            }
            """
            product_set_input = {
                'title': product.name,
                'descriptionHtml': body_html,
                'vendor': vendor,
                'productType': product.categ_id.name or '',
                'status': 'ACTIVE',
                'tags': tags,
            }

            images = self._get_product_images(product)
            if images:
                product_set_input['files'] = [
                    {'originalSource': f"data:image/jpeg;base64,{img}", 'contentType': 'IMAGE'}
                    for img in images
                ]

            data = config._graphql(create_mutation, {'synchronous': True, 'input': product_set_input})
            if not data:
                product.write({'shopify_sync_status': 'error', 'shopify_sync_error': 'productSet mislukt'})
                return False

            errors = data.get('productSet', {}).get('userErrors', [])
            if errors:
                _logger.error(f"productSet fout voor {product.name}: {errors}")
                product.write({'shopify_sync_status': 'error', 'shopify_sync_error': str(errors)})
                return False

            shopify_product = data.get('productSet', {}).get('product', {})
            shopify_product_id = shopify_product.get('legacyResourceId')
            shopify_variants = shopify_product.get('variants', {}).get('nodes', [])
            if shopify_variants:
                sv = shopify_variants[0]
                variant.write({
                    'shopify_variant_id': str(sv.get('legacyResourceId')),
                    'shopify_inventory_item_id': str(sv.get('inventoryItem', {}).get('legacyResourceId', '')),
                })
                if variant.shopify_variant_id:
                    self._update_variant_price(config, shopify_product_id, variant, price)

        product.write({
            'shopify_product_id': str(shopify_product_id),
            'shopify_last_sync': fields.Datetime.now(),
            'shopify_sync_status': 'synced',
            'shopify_sync_error': False,
        })
        _logger.info(f"Enkelvoudig product {product.name} gesynchroniseerd")

        if config.sync_inventory:
            self.sync_inventory_to_shopify(product.id, config)

        return True

    @api.model
    def _update_variant_price(self, config, shopify_product_id, variant, price):
        """Update de prijs van een variant via productVariantsBulkUpdate."""
        mutation = """
        mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            userErrors {
              field
              message
            }
          }
        }
        """
        variables = {
            'productId': f"gid://shopify/Product/{shopify_product_id}",
            'variants': [{
                'id': f"gid://shopify/ProductVariant/{variant.shopify_variant_id}",
                'price': str(price),
                'inventoryPolicy': 'CONTINUE' if config.allow_backorder else 'DENY',
            }]
        }
        data = config._graphql(mutation, variables)
        if data:
            errors = data.get('productVariantsBulkUpdate', {}).get('userErrors', [])
            if errors:
                _logger.warning(f"Variant prijs update fout: {errors}")

    @api.model
    def _sync_product_with_variants(self, config, product, shopify_product_id, vendor, tags, body_html):
        """Sync product met varianten via productSet."""
        mutation = """
        mutation productSet($synchronous: Boolean!, $input: ProductSetInput!) {
          productSet(synchronous: $synchronous, input: $input) {
            product {
              id
              legacyResourceId
              variants(first: 100) {
                nodes {
                  id
                  legacyResourceId
                  sku
                  selectedOptions {
                    name
                    value
                  }
                  inventoryItem {
                    id
                    legacyResourceId
                  }
                }
              }
            }
            userErrors {
              field
              message
              code
            }
          }
        }
        """

        product_set_input = {
            'title': product.name,
            'descriptionHtml': body_html,
            'vendor': vendor,
            'productType': product.categ_id.name or '',
            'status': 'ACTIVE',
            'tags': tags,
        }

        if shopify_product_id:
            product_set_input['id'] = f"gid://shopify/Product/{shopify_product_id}"

        # Opties
        options = []
        for line in product.attribute_line_ids[:3]:
            option_values = [{'name': value.name} for value in line.product_template_value_ids]
            options.append({'name': line.attribute_id.name, 'values': option_values})
        if options:
            product_set_input['productOptions'] = options

        # Varianten
        variants = []
        for variant in product.product_variant_ids:
            price = self._get_price(variant, config)
            variant_input = {
                'price': str(price),
                'inventoryPolicy': 'CONTINUE' if config.allow_backorder else 'DENY',
            }

            if variant.default_code:
                variant_input['sku'] = variant.default_code
            if hasattr(variant, 'barcode') and variant.barcode:
                variant_input['barcode'] = variant.barcode
            if variant.shopify_variant_id:
                variant_input['id'] = f"gid://shopify/ProductVariant/{variant.shopify_variant_id}"

            if variant.product_template_attribute_value_ids:
                option_values = [
                    {'name': attr_value.name, 'optionName': attr_value.attribute_id.name}
                    for attr_value in variant.product_template_attribute_value_ids
                ]
                variant_input['optionValues'] = option_values

            variants.append(variant_input)

        if variants:
            product_set_input['variants'] = variants

        images = self._get_product_images(product)
        if images and not shopify_product_id:
            product_set_input['files'] = [
                {'originalSource': f"data:image/jpeg;base64,{img}", 'contentType': 'IMAGE'}
                for img in images
            ]

        data = config._graphql(mutation, {'synchronous': True, 'input': product_set_input})
        if data:
            errors = data.get('productSet', {}).get('userErrors', [])
            if errors:
                _logger.error(f"productSet fout voor {product.name}: {errors}")
                product.write({'shopify_sync_status': 'error', 'shopify_sync_error': str(errors)})
                return False

            shopify_product = data.get('productSet', {}).get('product', {})
            shopify_product_legacy_id = shopify_product.get('legacyResourceId')
            shopify_variants = shopify_product.get('variants', {}).get('nodes', [])
            self._match_shopify_variants_graphql(product, shopify_variants)

            product.write({
                'shopify_product_id': str(shopify_product_legacy_id),
                'shopify_last_sync': fields.Datetime.now(),
                'shopify_sync_status': 'synced',
                'shopify_sync_error': False,
            })
            _logger.info(f"Product met varianten {product.name} gesynchroniseerd: {shopify_product_legacy_id}")

            if config.sync_inventory:
                self.sync_inventory_to_shopify(product.id, config)

            return True

        return False

    @api.model
    def _set_product_status(self, config, shopify_product_id, status, product):
        mutation = """
        mutation productUpdate($product: ProductUpdateInput!) {
          productUpdate(product: $product) {
            product {
              id
              status
            }
            userErrors {
              field
              message
            }
          }
        }
        """
        variables = {
            'product': {
                'id': f"gid://shopify/Product/{shopify_product_id}",
                'status': status,
            }
        }
        data = config._graphql(mutation, variables)
        if data:
            errors = data.get('productUpdate', {}).get('userErrors', [])
            if errors:
                _logger.error(f"Product status fout: {errors}")
                product.write({'shopify_sync_status': 'error', 'shopify_sync_error': str(errors)})
                return False
            product.write({
                'shopify_sync_status': 'synced',
                'shopify_last_sync': fields.Datetime.now(),
            })
            return True
        return False

    @api.model
    def _match_shopify_variants_graphql(self, product, shopify_variants):
        shopify_by_sku = {}
        shopify_by_options = {}

        for sv in shopify_variants:
            sku = sv.get('sku', '')
            if sku:
                shopify_by_sku[sku] = sv
            option_parts = [
                opt['value']
                for opt in sv.get('selectedOptions', [])
                if opt['value'] != 'Default Title'
            ]
            if option_parts:
                option_key = '|'.join(option_parts)
                shopify_by_options[option_key] = sv

        for variant in product.product_variant_ids:
            matched = None

            if variant.shopify_variant_id:
                for sv in shopify_variants:
                    if str(sv.get('legacyResourceId')) == str(variant.shopify_variant_id):
                        matched = sv
                        break

            if not matched and variant.default_code:
                matched = shopify_by_sku.get(variant.default_code)

            if not matched:
                option_key = self._get_variant_option_key(variant)
                if option_key:
                    matched = shopify_by_options.get(option_key)

            if matched:
                inventory_item_legacy_id = matched.get('inventoryItem', {}).get('legacyResourceId', '')
                variant.write({
                    'shopify_variant_id': str(matched.get('legacyResourceId')),
                    'shopify_inventory_item_id': str(inventory_item_legacy_id),
                })
                _logger.info(f"Variant {variant.name} gekoppeld: {matched.get('legacyResourceId')}")
            else:
                _logger.warning(f"Geen Shopify variant gevonden voor {variant.name}")
