import kopf
import kubernetes.client
from kubernetes.client.rest import ApiException
import logging

def sync_secret(secret_name, namespace, secret_data):
    api = kubernetes.client.CoreV1Api()
    namespaces = api.list_namespace().items 

    for ns in namespaces:
        ns_name = ns.metadata.name
        if ns_name == namespace: 
            continue
        logging.info(f"Synching {secret_name} in {ns_name}...")
        try:    
            api.read_namespaced_secret(secret_name, ns_name)
            api.delete_namespaced_secret(secret_name, ns_name)
        except ApiException as e:
            if e.status != 404:
                kopf.exception(e)
        new_secret = kubernetes.client.V1Secret(
            metadata=kubernetes.client.V1ObjectMeta(
                name=secret_name,
                labels={
                    "kss-operator/sync": "synched" 
                }
            ),
            data=secret_data.data,
            type=secret_data.type
        )
        api.create_namespaced_secret(namespace=ns_name, body=new_secret)
                
def delete_synced_secrets(secret_name):
    api = kubernetes.client.CoreV1Api()
    namespaces = api.list_namespace().items

    for ns in namespaces:
        ns_name = ns.metadata.name
        try:
            api.read_namespaced_secret(secret_name, ns_name)
            logging.info(f"Deleting {secret_name} from {ns_name}...")
            api.delete_namespaced_secret(secret_name, ns_name)
        except ApiException as e:
            if e.status != 404:
                kopf.exception(e)



@kopf.on.create('v1', 'Secret', labels={"kss-operator/sync": "sync"})
@kopf.on.update('v1', 'Secret', labels={"kss-operator/sync": "sync"})
def sync_secret_handler(spec, meta, namespace, **kwargs):
    secret_name = meta['name']
    api = kubernetes.client.CoreV1Api()
    secret = api.read_namespaced_secret(secret_name, namespace)
    logging.info(f"Secret {secret_name} created in the {namespace} namespace, syncing with all other namespaces.")

    sync_secret(secret_name, namespace, secret)

@kopf.on.delete('v1', 'Secret', labels={"kss-operator/sync": "sync"})
def delete_synced_secret_handler(meta, namespace, **kwargs):
    secret_name = meta['name']
    logging.info(f"Secret {secret_name} deleted from the {namespace} namepsace, removing from all other namespaces.")
    delete_synced_secrets(secret_name)