def present_posts(connection, username, message_ids, **kwargs):
    result = {}
    for message_id in message_ids:
        row = connection.execute('SELECT text FROM posts WHERE external_id=?', (str(message_id),)).fetchone()
        result[str(message_id)] = {'status': 'PRESENT', 'text': row['text'],
                                   'url': f'https://t.me/{username}/{message_id}', 'content_sha256': 'fixture'}
    return result
