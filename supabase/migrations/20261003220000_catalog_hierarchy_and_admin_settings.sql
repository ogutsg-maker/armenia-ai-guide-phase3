-- Persist the live 22-direction / 320-subdirection hierarchy repair.
-- master_categories/categories remain the source of legacy direction/subdirection settings;
-- aig_catalog_categories is the runtime hierarchy used by AI and services.

INSERT INTO aig_catalog_categories (parent_id,name_am,name_ru,name_en,slug,active)
SELECT NULL,m.name_am,m.name_ru,m.name_en,m.slug,m.is_active
FROM master_categories m
WHERE NOT EXISTS (
  SELECT 1 FROM aig_catalog_categories c WHERE c.slug=m.slug
);

UPDATE aig_catalog_categories root
SET active=m.is_active,
    name_am=m.name_am,
    name_ru=m.name_ru,
    name_en=m.name_en
FROM master_categories m
WHERE root.parent_id IS NULL AND root.slug=m.slug;

UPDATE aig_catalog_categories child
SET parent_id=root.id,
    active=c.is_active,
    name_am=c.name_am,
    name_ru=c.name_ru,
    name_en=c.name_en,
    slug=c.slug
FROM categories c
JOIN master_categories m ON m.id=c.master_category_id
JOIN aig_catalog_categories root ON root.slug=m.slug AND root.parent_id IS NULL
WHERE child.slug=c.slug;

UPDATE aig_direction_documents d
SET catalog_category_id=root.id
FROM aig_catalog_categories child
JOIN aig_catalog_categories root ON root.id=child.parent_id
WHERE d.catalog_category_id=child.id;

UPDATE aig_services s
SET direction_category_id=child.parent_id
FROM aig_catalog_categories child
WHERE s.catalog_category_id=child.id
  AND child.parent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS aig_catalog_categories_parent_active_idx
  ON aig_catalog_categories(parent_id,active);

CREATE INDEX IF NOT EXISTS aig_services_direction_status_idx
  ON aig_services(direction_category_id,status);
