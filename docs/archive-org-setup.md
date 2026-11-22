# Internet Archive setup

## 1. Create the account

1. Create an account at <https://archive.org/account/signup>.
2. Verify the email address.
3. Sign in and open <https://archive.org/account/s3.php>.
4. Generate IAS3 keys and copy the access key and secret key into Kaggle Secrets
   or local environment variables:

```bash
export IA_ACCESS_KEY="..."
export IA_SECRET_KEY="..."
```

Never commit these values to the repository or notebook output.

## 2. Confirm the interim collection

The default collection is Community Video:

```toml
collection = "opensource_movies"
```

This is a general archive.org collection. It is not run by The Plain Bagel and it
does not imply endorsement. The item's rights metadata and the archiver's contacts
must make the non-commercial permission and all-rights-reserved status clear.

## 3. Pre-clear the preservation plan

Internet Archive discourages mirroring content that is still available online
unless there is a preservation reason, and it may remove items or lock uploader
accounts. Before bulk upload, contact `info@archive.org` and the creator. Explain:

- the public YouTube source;
- the written non-commercial permission;
- why preservation is appropriate;
- the intended dedicated collection;
- the item identifier scheme;
- the rights statement and takedown contact.

Do not claim that this tool or the archiver has authority to license the work.

## 4. Request a dedicated collection

After 50+ items are published, run:

```bash
python -m plainbagel_archiver collection-request
```

Review the generated list, verify the links, and send the request to
`info@archive.org`. The current suggestion is:

- collection name: `The Plain Bagel Archive`
- identifier: `theplainbagel-archive`
- subject: `The Plain Bagel`

Wait for archive.org to create the collection. Do not change `config.toml` to a
non-existent collection; `run` checks the collection exists before uploading.

## 5. Migrate existing items

After the collection exists:

```bash
python -m plainbagel_archiver migrate-collection --collection theplainbagel-archive
```

This adds the new collection while retaining existing membership. Then update
`config.toml`:

```toml
collection = "theplainbagel-archive"
```

Future items will upload directly to the dedicated collection.

## 6. Rights and privacy

- Publish only the permission summary, not the complete private correspondence.
- Keep the creator's rights and the archiver's email visible in item metadata.
- Do not apply a Creative Commons license or mark the items public domain.
- Respond promptly to creator or archive.org correction/removal requests.
