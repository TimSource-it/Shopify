def _fetch_locations(self):
    """Haal alle actieve Shopify locaties op en sla op als mapping."""
    try:
        url = f"{self.shop_url}/admin/api/2025-01/locations.json"
        response = requests.get(url, headers=self._get_headers(), timeout=10)
        if response.status_code == 200:
            locations = response.json().get('locations', [])
            default_warehouse = self.env['stock.warehouse'].search([], limit=1)

            for location in locations:
                if not location.get('active'):
                    continue
                existing = self.env['shopify.location'].search([
                    ('config_id', '=', self.id),
                    ('shopify_location_id', '=', str(location['id'])),
                ], limit=1)
                if not existing:
                    self.env['shopify.location'].create({
                        'config_id': self.id,
                        'shopify_location_id': str(location['id']),
                        'shopify_location_name': location.get('name', ''),
                        'warehouse_id': default_warehouse.id if default_warehouse else False,
                        'sync_inventory': True,
                    })
                    _logger.info(f"Locatie aangemaakt: {location.get('name')}")

            if locations:
                active_locations = [l for l in locations if l.get('active')]
                if active_locations:
                    self.shopify_location_id = str(active_locations[0]['id'])

    except Exception as e:
        _logger.error(f"Locaties ophalen mislukt: {e}")
