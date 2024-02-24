for NUM in $(seq 1 16)
do
    echo ${NUM}+' is mounting'
    dir=/data/data${NUM}
    if [ ! -d "$dir" ]; then
        mkdir "$dir"
    fi
    sudo mkfs.ext4 /dev/nvme${NUM}n1
    sudo mount /dev/nvme${NUM}n1 /data/data${NUM}
done
sudo chmod -R a+rwx /data/