DELETE FROM users
WHERE id IN (SELECT user_id FROM basic_auth_identities);

DROP TABLE basic_auth_identities;
